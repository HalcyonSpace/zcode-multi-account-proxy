import { afterAll, afterEach, beforeEach, describe, expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  __resetCaptchaChildDispatchForTest,
  __setInProcessSolverForTest,
  prefetchCaptchaChild,
  solveViaChildOrInProcess,
} from "./captcha-child-dispatch.js";
import { __resetCaptchaChildEntryForTest } from "./captcha-child-entry.js";

// The child dispatch must: (1) mint real tokens through a REAL short-lived
// child process (fixture entry, no happy-dom / no network), (2) bank surplus
// tokens from a batch, (3) degrade to the in-process seam when the entry
// cannot be spawned. The in-process seam is used instead of mock.module —
// a Bun module mock is process-wide and leaks a partial export surface into
// later test files (same reason captcha-worker-dispatch.test.ts avoids it).

const fixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), "cap-child-"));
const fixturePath = path.join(fixtureDir, "fixture-child.ts");

// Emits N line-delimited JSON tokens then a done line, echoing the stdin
// config so the test can assert identity actually reached the child.
fs.writeFileSync(
  fixturePath,
  [
    "let raw = '';",
    "for await (const chunk of Bun.stdin.stream()) { raw += new TextDecoder().decode(chunk); if (raw.includes('\\n')) break; }",
    "const cfg = JSON.parse(raw.trim());",
    "const n = Math.max(1, Math.min(Number(cfg.count) || 1, 20));",
    "for (let i = 0; i < n; i += 1) {",
    "  process.stdout.write(JSON.stringify({ ok: true, param: `fixture:${cfg.scene}:${cfg.region}:${cfg.prefix}:${i}` }) + '\\n');",
    "}",
    "process.stdout.write(JSON.stringify({ done: true, ok: n, fail: 0 }) + '\\n');",
  ].join("\n"),
  "utf8",
);

// A child that exits without producing a token and prints a load-error shape.
const deadFixturePath = path.join(fixtureDir, "dead-child.ts");
fs.writeFileSync(
  deadFixturePath,
  "process.stderr.write('error: Cannot find module \"happy-dom\"\\n'); process.exit(1);\n",
  "utf8",
);

const prevEntry = process.env.CAPTCHA_CHILD_ENTRY;
const prevMode = process.env.CAPTCHA_SOLVER_MODE;
const prevBatch = process.env.CAPTCHA_CHILD_BATCH;

function useFixture(p: string): void {
  process.env.CAPTCHA_CHILD_ENTRY = p;
  delete process.env.CAPTCHA_SOLVER_MODE;
  __resetCaptchaChildEntryForTest();
  __resetCaptchaChildDispatchForTest();
}

describe("captcha child dispatch", () => {
  beforeEach(() => {
    delete process.env.CAPTCHA_SOLVER_MODE;
    delete process.env.CAPTCHA_CHILD_BATCH;
  });
  afterEach(() => {
    __setInProcessSolverForTest(null);
    if (prevEntry === undefined) delete process.env.CAPTCHA_CHILD_ENTRY;
    else process.env.CAPTCHA_CHILD_ENTRY = prevEntry;
    if (prevMode === undefined) delete process.env.CAPTCHA_SOLVER_MODE;
    else process.env.CAPTCHA_SOLVER_MODE = prevMode;
    if (prevBatch === undefined) delete process.env.CAPTCHA_CHILD_BATCH;
    else process.env.CAPTCHA_CHILD_BATCH = prevBatch;
    __resetCaptchaChildEntryForTest();
    __resetCaptchaChildDispatchForTest();
  });
  afterAll(() => {
    try { fs.rmSync(fixtureDir, { recursive: true, force: true }); } catch {}
  });

  test("mints a token through a real child process", async () => {
    useFixture(fixturePath);
    const param = await solveViaChildOrInProcess({ scene: "sc1", region: "rg1", prefix: "pf1" });
    expect(param).toBe("fixture:sc1:rg1:pf1:0");
  });

  test("passes identity config over stdin, never argv", async () => {
    useFixture(fixturePath);
    const param = await solveViaChildOrInProcess({ scene: "SECRETSCENE", region: "r", prefix: "p" });
    expect(param).toContain("SECRETSCENE");
  });

  test("banks surplus tokens from a batch and serves them before respawning", async () => {
    process.env.CAPTCHA_CHILD_BATCH = "3";
    useFixture(fixturePath);
    const first = await solveViaChildOrInProcess({ scene: "sc", region: "rg", prefix: "pf" });
    const second = await solveViaChildOrInProcess({ scene: "sc", region: "rg", prefix: "pf" });
    const third = await solveViaChildOrInProcess({ scene: "sc", region: "rg", prefix: "pf" });
    expect([first, second, third]).toEqual([
      "fixture:sc:rg:pf:0",
      "fixture:sc:rg:pf:1",
      "fixture:sc:rg:pf:2",
    ]);
  });

  test("never serves a surplus token to a different identity", async () => {
    process.env.CAPTCHA_CHILD_BATCH = "2";
    useFixture(fixturePath);
    await solveViaChildOrInProcess({ scene: "A", region: "rg", prefix: "pf" });
    // Different scene: the banked "A" surplus must not be reused.
    const other = await solveViaChildOrInProcess({ scene: "B", region: "rg", prefix: "pf" });
    expect(other).toBe("fixture:B:rg:pf:0");
  });

  test("degrades to in-process when the child entry cannot be spawned", async () => {
    useFixture(deadFixturePath);
    __setInProcessSolverForTest(async () => "in-process-token");
    const param = await solveViaChildOrInProcess({ scene: "s", region: "r", prefix: "p" });
    expect(param).toBe("in-process-token");
  });

  test("degrades to in-process when CAPTCHA_CHILD_ENTRY points at a missing file", async () => {
    useFixture(path.join(fixtureDir, "does-not-exist.ts"));
    __setInProcessSolverForTest(async () => "in-process-token");
    const param = await solveViaChildOrInProcess({ scene: "s", region: "r", prefix: "p" });
    expect(param).toBe("in-process-token");
  });

  test("CAPTCHA_SOLVER_MODE=inprocess skips the child entirely", async () => {
    process.env.CAPTCHA_CHILD_ENTRY = fixturePath;
    process.env.CAPTCHA_SOLVER_MODE = "inprocess";
    __resetCaptchaChildEntryForTest();
    __resetCaptchaChildDispatchForTest();
    __setInProcessSolverForTest(async () => "in-process-token");
    const param = await solveViaChildOrInProcess({ scene: "s", region: "r", prefix: "p" });
    expect(param).toBe("in-process-token");
  });

  test("propagates an in-child solve failure without degrading", async () => {
    const failPath = path.join(fixtureDir, "fail-child.ts");
    fs.writeFileSync(
      failPath,
      "process.stdout.write(JSON.stringify({ ok:false, error:'F008 duplicate' }) + '\\n');" +
        "process.stdout.write(JSON.stringify({ done:true, ok:0, fail:1 }) + '\\n');",
      "utf8",
    );
    useFixture(failPath);
    __setInProcessSolverForTest(async () => "should-not-be-used");
    await expect(
      solveViaChildOrInProcess({ scene: "s", region: "r", prefix: "p" }),
    ).rejects.toThrow(/F008/);
  });

  test("prefetch warms the queue so the next take needs no spawn", async () => {
    process.env.CAPTCHA_CHILD_BATCH = "2";
    useFixture(fixturePath);
    prefetchCaptchaChild({ scene: "sc", region: "rg", prefix: "pf" });
    await new Promise((r) => setTimeout(r, 400));
    const t = await solveViaChildOrInProcess({ scene: "sc", region: "rg", prefix: "pf" });
    expect(t.startsWith("fixture:sc:rg:pf:")).toBe(true);
  });

  test("kills a wedged child at the batch timeout and degrades to in-process", async () => {
    const wedged = path.join(fixtureDir, "wedged-child.ts");
    fs.writeFileSync(wedged, "setInterval(() => {}, 1000);\n", "utf8");
    process.env.CAPTCHA_CHILD_TIMEOUT_MS = "700";
    useFixture(wedged);
    __setInProcessSolverForTest(async () => "in-process-token");
    const t0 = Date.now();
    const param = await solveViaChildOrInProcess({ scene: "s", region: "r", prefix: "p" });
    expect(param).toBe("in-process-token");
    // The parent must not wait anywhere near the default 90s cap.
    expect(Date.now() - t0).toBeLessThan(10_000);
  });
});
