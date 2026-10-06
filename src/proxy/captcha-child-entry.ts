/**
 * captcha-child-entry.ts — resolve the on-disk entry for the short-lived
 * captcha child process.
 *
 * From source (`bun run src/index.ts serve`, the live/deployed shape) the entry
 * is simply the sibling captcha-child.ts, which `bun run` executes directly --
 * no build step, no bundling. In a compiled single-file binary there is no
 * on-disk .ts to spawn, so we report null and the dispatch degrades (worker
 * asset path, then in-process).
 */
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Resolve the .ts/.js entry to spawn, or null when not spawnable. */
export function resolveCaptchaChildEntrySync(): string | null {
  const override = process.env.CAPTCHA_CHILD_ENTRY?.trim();
  if (override) return existsSync(override) ? override : null;

  let here: string;
  try {
    here = path.dirname(fileURLToPath(import.meta.url));
  } catch {
    // Compiled binary: import.meta.url is a bunfs/virtual path.
    return null;
  }
  for (const name of ["captcha-child.ts", "captcha-child.js"]) {
    const candidate = path.join(here, name);
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

let cached: string | null | undefined;

/** Cached variant for the hot path. Cached `null` is permanent for the process. */
export async function captchaChildEntryPath(): Promise<string | null> {
  if (cached !== undefined) return cached;
  cached = resolveCaptchaChildEntrySync();
  return cached;
}

/** Test-only cache reset. */
export function __resetCaptchaChildEntryForTest(): void {
  cached = undefined;
}
