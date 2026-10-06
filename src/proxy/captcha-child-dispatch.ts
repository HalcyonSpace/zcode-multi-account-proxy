/**
 * Captcha solver CHILD-PROCESS dispatch.
 *
 * Why: happy-dom + the Aliyun SDK leave tens of MB resident per solve in the
 * JSC heap, and the reusable-window optimization deliberately keeps a window
 * alive between solves -- so the solving process climbs to ~780MB and never
 * gives it back (live measurement: solving account 779MB vs 120-195MB idle).
 * Solving is on the hot path (one token per upstream request batch), so the
 * solver cannot simply be dropped.
 *
 * Design: mint tokens in a SHORT-LIVED child (`bun run src/proxy/captcha-child.ts`),
 * hand it identity config on stdin as JSON, read tokens back as line-delimited
 * JSON on stdout, then let the process exit. Every byte of happy-dom/JSC heap
 * returns to the OS at exit; the parent keeps only the token strings.
 *
 * The child mints a small batch (CAPTCHA_CHILD_BATCH, default 2) and the
 * dispatch banks the surplus in a tiny prefetch queue, so the parent pays one
 * spawn per N tokens instead of one per token. The public surface is still the
 * single-token solve the pool already calls: `runCaptchaSolve(scene,region,prefix)`.
 *
 * Degradation (same shape as captcha-worker-dispatch.ts):
 *   child spawnable -> child
 *   child entry/load failure -> in-process (legacy path)
 *   CAPTCHA_SOLVER_MODE=inprocess -> in-process (A/B measurement arm)
 */
import { captchaChildEntryPath } from "./captcha-child-entry.js";

type InProcessFn = (req: { scene: string; region: string; prefix: string }) => Promise<string>;

export interface SolveRequest { scene: string; region: string; prefix: string; }

// Env-derived knobs are read lazily (not at module load) so tests and an
// in-process reconfiguration can change them without a fresh module registry.
/** Child wall-clock cap; the parent kills it. */
const childBatchTimeoutMs = () => Number(process.env.CAPTCHA_CHILD_TIMEOUT_MS || 90_000);
/** Per-solve deadline handed to the child (happy-dom's own stall detector). */
const childSolveTimeoutMs = () => Number(process.env.CAPTCHA_CHILD_SOLVE_TIMEOUT_MS || 25_000);
/** Concurrent children. 1 by default: the win is resident memory, not parallelism. */
const childConcurrency = () => Math.max(1, Number(process.env.CAPTCHA_CHILD_CONCURRENCY || 1));
/** Tokens minted per child spawn (surplus is banked for the next take). */
const childBatchSize = () => Math.max(1, Math.min(Number(process.env.CAPTCHA_CHILD_BATCH || 2), 20));

let inProcessOverride: InProcessFn | null = null;
let happyMod: { solveTraceless: InProcessFn } | null = null;

async function happy(): Promise<{ solveTraceless: InProcessFn }> {
  if (inProcessOverride) return { solveTraceless: inProcessOverride };
  if (!happyMod) {
    happyMod = (await import("./captcha-happy.js")) as { solveTraceless: InProcessFn };
  }
  return happyMod;
}

/** Child entry unusable — the only child failure that degrades. */
export class ChildUnavailableError extends Error {}

let lastNotedMode = "";
function noteMode(mode: string, line: string): void {
  if (mode === lastNotedMode) return;
  lastNotedMode = mode;
  try { process.stderr.write(line); } catch { /* noop */ }
}

/** Enabled unless CAPTCHA_SOLVER_MODE is explicitly in-process. */
export function captchaChildModeEnabled(): boolean {
  const raw = (process.env.CAPTCHA_SOLVER_MODE || "child").trim().toLowerCase();
  return raw !== "inprocess" && raw !== "in-process" && raw !== "inline";
}

// -- Concurrency gate (default 1 child at a time) ---------------------------
let running = 0;
const waiters: Array<() => void> = [];

async function acquireSlot(): Promise<void> {
  if (running < childConcurrency()) { running += 1; return; }
  await new Promise<void>((resolve) => waiters.push(resolve));
  running += 1;
}

function releaseSlot(): void {
  running -= 1;
  const next = waiters.shift();
  if (next) next();
}

// -- Prefetch queue ----------------------------------------------------------
// Tokens minted by the last child beyond the one that was handed out. Keyed by
// scene/region/prefix so a reconfigured pool never serves a stale-identity
// token. Bounded: one surplus batch is plenty (the pool keeps its own buffer).
interface QueuedToken { param: string; scene: string; region: string; prefix: string; at: number; }
const prefetchTtlMs = () => Number(process.env.CAPTCHA_CHILD_PREFETCH_TTL_MS || 60_000);
let surplus: QueuedToken[] = [];
let prefetchInFlight = false;

function takeSurplus(req: SolveRequest): string | null {
  const now = Date.now();
  surplus = surplus.filter((t) => now - t.at < prefetchTtlMs());
  const i = surplus.findIndex(
    (t) => t.scene === req.scene && t.region === req.region && t.prefix === req.prefix,
  );
  if (i < 0) return null;
  const [token] = surplus.splice(i, 1);
  return token.param;
}

function bankSurplus(params: string[], req: SolveRequest): void {
  if (params.length <= 1) return;
  const now = Date.now();
  surplus = surplus.filter((t) => now - t.at < prefetchTtlMs());
  for (const param of params.slice(1)) {
    surplus.push({ param, scene: req.scene, region: req.region, prefix: req.prefix, at: now });
  }
  // Only one surplus batch is ever useful; drop the oldest beyond that.
  const max = childBatchSize();
  if (surplus.length > max) surplus = surplus.slice(surplus.length - max);
}

/**
 * Warm the surplus queue in the background so the next take is instant.
 * Guarded by `prefetchInFlight` and the concurrency gate, so at most one
 * extra child is ever alive.
 */
export function prefetchCaptchaChild(req: SolveRequest): void {
  if (!captchaChildModeEnabled()) return;
  if (prefetchInFlight) return;
  const now = Date.now();
  const fresh = surplus.filter(
    (t) => now - t.at < prefetchTtlMs() &&
      t.scene === req.scene && t.region === req.region && t.prefix === req.prefix,
  );
  if (fresh.length >= childBatchSize()) return;
  prefetchInFlight = true;
  void (async () => {
    try {
      const entry = await captchaChildEntryPath();
      if (entry === null) return;
      const params = await runChild(entry, req, childBatchSize());
      bankSurplus(params, req);
    } catch { /* prefetch is best-effort */ } finally {
      prefetchInFlight = false;
    }
  })();
}

// -- Public API (single token, unchanged signature) --------------------------

/**
 * Mint one token. Serves a banked surplus token when available, otherwise
 * spawns a child for a batch and banks the rest.
 */
export async function solveViaChildOrInProcess(req: SolveRequest): Promise<string> {
  if (!captchaChildModeEnabled()) return (await happy()).solveTraceless(req);

  const banked = takeSurplus(req);
  if (banked) return banked;

  const entry = await captchaChildEntryPath();
  if (entry === null) {
    noteMode("in-process", "[captcha-solver] child entry unavailable — solving in-process\n");
    return (await happy()).solveTraceless(req);
  }
  try {
    const params = await runChild(entry, req, childBatchSize());
    bankSurplus(params, req);
    noteMode("child", "[captcha-solver] child-process solving active\n");
    return params[0]!;
  } catch (err) {
    if (err instanceof ChildUnavailableError) {
      noteMode("in-process", `[captcha-solver] ${err.message} — degrading to in-process solving\n`);
      return (await happy()).solveTraceless(req);
    }
    throw err;
  }
}

/** Test-only reset of cached module/mode/queue state. */
export function __resetCaptchaChildDispatchForTest(): void {
  happyMod = null;
  lastNotedMode = "";
  surplus = [];
  prefetchInFlight = false;
}

/** Test-only in-process substitution seam (mirrors the worker dispatch seam). */
export function __setInProcessSolverForTest(fn: InProcessFn | null): void {
  inProcessOverride = fn;
}

// -- Child runner ------------------------------------------------------------

/**
 * Spawn one child, feed stdin, collect stdout lines. Rejects with
 * ChildUnavailableError only for spawn/load failures; in-child solve failures
 * throw a normal Error so the pool's retry ladder (which owns F008/IP-block
 * classification) handles them.
 */
async function runChild(entry: string, req: SolveRequest, count: number): Promise<string[]> {
  await acquireSlot();
  const n = Math.max(1, Math.min(Number(count) || childBatchSize(), 20));
  const startedAt = Date.now();
  let proc: ReturnType<typeof Bun.spawn> | null = null;
  let killed = false;
  const kill = () => {
    if (!proc || killed) return;
    killed = true;
    try { proc.kill("SIGKILL"); } catch { /* already gone */ }
  };
  const timer = setTimeout(kill, childBatchTimeoutMs());

  try {
    try {
      // `bun run <entry>` from source: no build step. Identity travels on
      // stdin so no secret lands in argv (which is world-readable via `ps`).
      proc = Bun.spawn([runtimeBin(), "run", entry], {
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          ZCODE_CAPTCHA_CHILD: "1",
          // Reuse the window *within* a batch (amortizes the DOM boot), then
          // the whole window dies with the process.
          CAPTCHA_WINDOW_REUSE: process.env.CAPTCHA_CHILD_WINDOW_REUSE ?? "1",
        },
      });
    } catch (err) {
      throw new ChildUnavailableError(`captcha child spawn failed: ${(err as Error).message}`);
    }

    const stdin = proc.stdin;
    if (!stdin || typeof stdin === "number") {
      throw new ChildUnavailableError("captcha child stdin not open");
    }

    stdin.write(JSON.stringify({
      scene: req.scene,
      region: req.region,
      prefix: req.prefix,
      count: n,
      timeoutMs: childSolveTimeoutMs(),
    }) + "\n");
    try { await (stdin as any).flush?.(); } catch { /* noop */ }
    try { (stdin as any).end?.(); } catch { /* noop */ }

    const params: string[] = [];
    let solveError = "";
    let sawOutput = false;
    const decoder = new TextDecoder();
    let buf = "";

    const drained = (async () => {
      for await (const chunk of proc!.stdout as ReadableStream<Uint8Array>) {
        sawOutput = true;
        buf += decoder.decode(chunk, { stream: true });
        let nl: number;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try {
            const msg = JSON.parse(line) as { param?: string; done?: boolean; error?: string };
            if (typeof msg.param === "string" && msg.param) params.push(msg.param);
            else if (msg.error) solveError ||= msg.error;
          } catch { /* non-JSON stdout diagnostics: ignore */ }
        }
      }
    })().catch(() => {});

    const exitCode = await Promise.race([
      proc.exited,
      // Hard cap even if the child wedges with the pipe open.
      new Promise<number>((resolve) => {
        const t = setTimeout(() => { kill(); resolve(-1); }, childBatchTimeoutMs());
        (t as { unref?: () => void }).unref?.();
      }),
    ]);
    await drained;

    if (params.length > 0) return params;

    let stderrText = "";
    try { stderrText = await new Response(proc.stderr as ReadableStream<Uint8Array>).text(); } catch { /* noop */ }
    const blob = `${solveError} ${stderrText}`.trim();
    if (!sawOutput || isEntryUnavailable(blob)) {
      throw new ChildUnavailableError(
        `captcha child produced no token (exit ${exitCode}): ${blob.slice(0, 300) || "no output"}`,
      );
    }
    throw new Error(
      `captcha child failed all ${n} solve(s): ${blob.slice(0, 300) || `exit ${exitCode}`} (${Date.now() - startedAt}ms)`,
    );
  } finally {
    clearTimeout(timer);
    kill();
    releaseSlot();
  }
}

/** Module-not-found / unloadable-entry signatures => degrade, not hard fail. */
function isEntryUnavailable(text: string): boolean {
  return /cannot find module|module not found|ERR_MODULE_NOT_FOUND|no such file|failed to resolve/i.test(text);
}

/** The runtime to spawn: the bun running us, so the child is never a mismatch. */
function runtimeBin(): string {
  return process.env.CAPTCHA_CHILD_RUNTIME || process.execPath || "bun";
}
