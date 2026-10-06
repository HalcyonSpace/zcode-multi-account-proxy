/**
 * Solver backend dispatch.
 *
 * Backend (ZCODE_CAPTCHA_BACKEND): "happy" (default) — the happy-dom solver
 * in src/proxy/captcha-happy.ts.
 *
 * Execution mode (CAPTCHA_SOLVER_MODE, default "child"):
 *   child      — mint tokens in a SHORT-LIVED child process (captcha-child.ts)
 *                so happy-dom/JSC heap never accumulates in the long-lived
 *                proxy (see captcha-child-dispatch.ts).
 *   inprocess  — the legacy path: one worker thread per solve, degrading to
 *                in-process happy-dom when the bundled worker asset is absent
 *                (captcha-worker-dispatch.ts). Kept as the A/B reference and
 *                as the only path inside a compiled single-file binary.
 */
import { solveViaWorkerOrInProcess } from "./captcha-worker-dispatch.js";
import {
  captchaChildModeEnabled,
  prefetchCaptchaChild,
  solveViaChildOrInProcess,
} from "./captcha-child-dispatch.js";

const BACKEND = process.env.ZCODE_CAPTCHA_BACKEND?.trim().toLowerCase() || "happy";

export async function runCaptchaSolve(scene: string, region: string, prefix: string): Promise<string> {
  if (BACKEND !== "happy") {
    throw new Error(`captcha backend "${BACKEND}" is not available; use ZCODE_CAPTCHA_BACKEND=happy`);
  }
  if (captchaChildModeEnabled()) {
    return solveViaChildOrInProcess({ scene, region, prefix });
  }
  return solveViaWorkerOrInProcess({ scene, region, prefix });
}

/**
 * Best-effort warm-up for the child mode's surplus queue. A no-op in the
 * worker/in-process mode, so callers can invoke it unconditionally.
 */
export function prefetchCaptchaSolve(scene: string, region: string, prefix: string): void {
  if (BACKEND !== "happy" || !captchaChildModeEnabled()) return;
  prefetchCaptchaChild({ scene, region, prefix });
}

/** Worker-per-solve needs no concurrency plumbing — kept for the pool API. */
export function setCaptchaSolverConcurrency(_n: number): void {}

/** Nothing long-lived to shut down: children are killed per batch. */
export function shutdownCaptchaSolver(): void {}

export function captchaSolverConcurrency(): number {
  return Number(process.env.CAPTCHA_DAEMON_CONCURRENCY || 4);
}
