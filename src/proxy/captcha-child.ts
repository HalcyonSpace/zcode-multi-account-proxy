/**
 * Captcha solver CHILD ENTRY (short-lived process).
 *
 * Protocol (line-delimited JSON):
 *   stdin  (one line): {"scene","region","prefix","count","timeoutMs"}
 *   stdout (per token): {"ok":true,"param":"..."}  |  {"ok":false,"error":"..."}
 *   stdout (final)   : {"done":true,"ok":<n>,"fail":<n>}
 *   stderr: diagnostics only.
 *
 * The process loads happy-dom, mints `count` tokens, flushes stdout and exits,
 * so every byte of happy-dom/JSC heap is returned to the OS at exit. The
 * parent keeps only token strings.
 */
import { solveTraceless } from "./captcha-happy.js";

interface ChildConfig {
  scene: string;
  region: string;
  prefix: string;
  count: number;
  timeoutMs?: number;
}

const emit = (obj: unknown) => {
  process.stdout.write(JSON.stringify(obj) + "\n");
};

async function readStdinLine(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of Bun.stdin.stream()) {
    chunks.push(Buffer.from(chunk));
    if (Buffer.concat(chunks).includes(0x0a)) break;
  }
  return Buffer.concat(chunks).toString("utf8").split("\n")[0] ?? "";
}

const raw = (await readStdinLine()).trim();
if (!raw) {
  emit({ ok: false, error: "captcha child: empty stdin config" });
  process.exit(2);
}

let cfg: ChildConfig;
try {
  cfg = JSON.parse(raw) as ChildConfig;
} catch (err) {
  emit({ ok: false, error: `captcha child: bad stdin config: ${(err as Error).message}` });
  process.exit(2);
}

const count = Math.max(1, Math.min(Number(cfg.count) || 1, 20));
let ok = 0;
let fail = 0;

// Sequential on purpose: captcha-happy's reusable-window state is global to
// the process, and concurrent solves there race on it (the reason the worker
// path went one-solve-per-worker). Sequential keeps the ~48% window-reuse CPU
// win; the parent overlaps batches across children if it needs throughput.
for (let i = 0; i < count; i += 1) {
  try {
    const param = await solveTraceless({
      scene: cfg.scene,
      region: cfg.region,
      prefix: cfg.prefix,
      timeoutMs: cfg.timeoutMs,
    });
    ok += 1;
    emit({ ok: true, param });
  } catch (err) {
    fail += 1;
    emit({ ok: false, error: String((err as Error)?.message ?? err).slice(0, 400) });
  }
}

emit({ done: true, ok, fail });
try { await (Bun.stdout as any).flush?.(); } catch { /* ignore */ }
process.exit(ok > 0 ? 0 : 1);
