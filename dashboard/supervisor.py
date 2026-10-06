#!/usr/bin/env python3
"""
ZCode Quota Auto-Sleep Supervisor.

Monitors account token balances. When an account's promo/daily quota is fully
drained, it gracefully parks (stops) the service unit/container until the
renewal timestamp arrives, saving memory and eliminating upstream 429 quota errors.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
STORES_DIR = Path(os.environ.get("STORES_DIR", str(ROOT / "stores")))
REG_FILE = DATA_DIR / "accounts.json"
STATE_FILE = DATA_DIR / "supervisor-state.json"
PID_FILE = DATA_DIR / "supervisor.pid"

BACKEND = os.environ.get("ZCODE_BACKEND", "docker").lower()
RENEWAL_BUFFER_S = 90
DEFAULT_PARK_HOURS = 2


def log(msg):
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)


def load_reg():
    try:
        return json.loads(REG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"instances": []}


def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def has_creds(aid):
    return (STORES_DIR / f"account-{aid}" / "credentials.json").exists()


def container_running(aid):
    if BACKEND == "docker":
        name = f"zcode-proxy-{aid}"
        try:
            r = subprocess.run(
                ["docker", "inspect", name, "--format", "{{.State.Running}}"],
                capture_output=True, text=True, timeout=15
            )
            return r.stdout.strip() == "true"
        except Exception:
            return False
    elif BACKEND == "systemd":
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", f"zcode-proxy-{aid}.service"],
                capture_output=True, text=True, timeout=15
            )
            return r.stdout.strip() == "active"
        except Exception:
            return False
    return True


def control(aid, action):
    """action: 'start' or 'stop'"""
    if BACKEND == "docker":
        name = f"zcode-proxy-{aid}"
        cmd = ["docker", "compose", "up", "-d", name] if action == "start" else ["docker", "compose", "stop", name]
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:
            return False, str(e)
    elif BACKEND == "systemd":
        svc = f"zcode-proxy-{aid}.service"
        try:
            r = subprocess.run(["systemctl", "--user", action, svc], capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:
            return False, str(e)
    return True, "ok"


def probe(inst_cfg, timeout=10):
    port = inst_cfg.get("port", 8080)
    key = inst_cfg.get("apiKey", "")
    host = os.environ.get("ZCODE_PROXY_HOST", "127.0.0.1")
    url = f"http://{host}:{port}/quota"

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}"} if key else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            bals = data.get("balances")
            if not isinstance(bals, list) or len(bals) == 0:
                return False, "no balances returned"
            return True, bals
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def earliest_renewal(balances):
    now = time.time()
    candidates = []
    for b in balances:
        exp = b.get("expiresAt")
        if exp and exp > now:
            candidates.append(exp)
    if candidates:
        return min(candidates) + RENEWAL_BUFFER_S
    return now + (DEFAULT_PARK_HOURS * 3600)


def sweep(dry=False, verbose=True):
    reg = load_reg()
    state = load_state()
    now = time.time()
    results = {"kept": [], "slept": [], "woke": [], "watching": [], "failed": []}

    for inst_cfg in reg.get("instances", []):
        aid = inst_cfg["id"]
        if not has_creds(aid):
            continue

        running = container_running(aid)
        inst_state = state.get(aid, {})

        # Check waking
        if inst_state.get("state") == "asleep":
            wake_at = inst_state.get("wakeAt", 0)
            if now >= wake_at:
                if verbose:
                    log(f"[{aid}] WAKING (renewal passed)")
                if not dry:
                    control(aid, "start")
                    del state[aid]
                results["woke"].append(aid)
            else:
                if verbose:
                    dt = datetime.fromtimestamp(wake_at, tz=timezone.utc).strftime("%H:%M:%S UTC")
                    log(f"[{aid}] Still asleep until {dt}")
                results["watching"].append(aid)
            continue

        if not running:
            continue

        # Probe quota
        ok, res = probe(inst_cfg)
        if not ok:
            results["failed"].append((aid, res))
            continue

        total_rem = sum(b.get("remainingUnits", 0) for b in res)
        if total_rem == 0:
            wake_time = earliest_renewal(res)
            dt = datetime.fromtimestamp(wake_time, tz=timezone.utc).strftime("%H:%M:%S UTC")
            if verbose:
                log(f"[{aid}] SLEEPING until {dt} (all quota buckets drained)")
            if not dry:
                control(aid, "stop")
                state[aid] = {"state": "asleep", "wakeAt": wake_time, "sleptAt": now}
            results["slept"].append(aid)
        else:
            results["kept"].append(aid)

    if not dry:
        save_state(state)
    return results


def main():
    parser = argparse.ArgumentParser(description="ZCode Quota Auto-Sleep Supervisor")
    parser.add_argument("--once", action="store_true", help="Run one sweep and exit")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without executing")
    parser.add_argument("--interval", type=int, default=60, help="Sweep cadence in seconds (default 60)")
    args = parser.parse_args()

    if args.once:
        sweep(dry=args.dry_run, verbose=True)
        return

    log(f"Supervisor started (cadence: {args.interval}s, backend: {BACKEND})")
    try:
        while True:
            try:
                sweep(dry=args.dry_run, verbose=False)
            except Exception as e:
                log(f"Sweep error: {e}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("Supervisor stopped.")


if __name__ == "__main__":
    main()
