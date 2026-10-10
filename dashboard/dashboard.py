#!/usr/bin/env python3
"""
ZCode Multi-Account Management Dashboard.

Features:
  - Web UI for managing single or multiple ZCode proxy accounts
  - One-click headless OAuth login (extracts upstream auth URL for browser approval)
  - Real-time token quota & balance tracking across models and buckets
  - Container / process lifecycle management (start, stop, restart, auto-sleep status)
  - Support for Docker Compose, systemd, and standalone process backends
"""

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Paths
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
STORES_DIR = Path(os.environ.get("STORES_DIR", str(ROOT / "stores")))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", str(ROOT / "logs")))
REG_FILE = DATA_DIR / "accounts.json"
SUPERVISOR_STATE = DATA_DIR / "supervisor-state.json"

# Backend engine: 'docker' (default), 'systemd', or 'process'
BACKEND = os.environ.get("ZCODE_BACKEND", "docker").lower()
BUN_BIN = os.environ.get("ZCODE_BUN", "bun")
APP_DIR = Path(os.environ.get("ZCODE_APP_DIR", str(ROOT)))

# Caching & singleflight configuration
SNAP_TTL = max(0.0, float(os.environ.get("ZCODE_DASH_TTL", "15")))
FETCH_BUDGET = 25.0

_lock = threading.Lock()
_jobs = []
_snap_lock = threading.Lock()
_snap_data = None
_snap_at = 0.0

_URL_RE = re.compile(r"https?://\S+")


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def init_registry_if_missing():
    ensure_dirs()
    if not REG_FILE.exists():
        default_reg = {
            "app": "zcode-proxy-fleet",
            "count": 1,
            "dashboardToken": secrets.token_urlsafe(24),
            "instances": [
                {
                    "id": "01",
                    "port": 8080,
                    "store": "stores/account-01",
                    "apiKey": secrets.token_urlsafe(24),
                    "credentialSecret": secrets.token_urlsafe(32),
                    "proxy": None,
                    "signedIn": False
                }
            ]
        }
        REG_FILE.write_text(json.dumps(default_reg, indent=2), encoding="utf-8")


def load_reg():
    init_registry_if_missing()
    try:
        return json.loads(REG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"instances": []}


def save_reg(reg):
    ensure_dirs()
    REG_FILE.write_text(json.dumps(reg, indent=2), encoding="utf-8")


def token():
    with _lock:
        reg = load_reg()
        if not reg.get("dashboardToken"):
            reg["dashboardToken"] = secrets.token_urlsafe(24)
            save_reg(reg)
        return reg["dashboardToken"]


def inst(reg, aid):
    for i in reg.get("instances", []):
        if i["id"] == aid:
            return i
    return None


def store_creds(aid):
    return STORES_DIR / f"account-{aid}" / "credentials.json"


def has_creds(aid):
    return store_creds(aid).exists()


def invalidate_snapshot():
    global _snap_data, _snap_at
    with _snap_lock:
        _snap_data = None
        _snap_at = 0.0


def load_supervisor_state():
    try:
        if SUPERVISOR_STATE.exists():
            return json.loads(SUPERVISOR_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ----------------------------------------------------------- Lifecycle / Backend
def run_command(cmd, log_file=None, timeout=180, env=None, cwd=None):
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        out = (r.stdout + "\n" + r.stderr).strip()
        if log_file:
            Path(log_file).write_text(out, encoding="utf-8")
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def container_state(aid):
    """Query service/container state across backends."""
    if BACKEND == "docker":
        name = f"zcode-proxy-{aid}"
        rc, out = run_command(["docker", "inspect", name, "--format", "{{.State.Status}}"])
        return out.strip().lower() if rc == 0 else "stopped"
    elif BACKEND == "systemd":
        rc, out = run_command(["systemctl", "--user", "is-active", f"zcode-proxy-{aid}.service"])
        st = out.strip().lower()
        if st == "active":
            return "running"
        elif st in ("inactive", "deactivating"):
            return "stopped"
        return st or "unknown"
    return "running" if has_creds(aid) else "stopped"


def control_service(aid, action):
    """Start, stop, or restart a service unit across backends."""
    if BACKEND == "docker":
        name = f"zcode-proxy-{aid}"
        if action == "start":
            return run_command(["docker", "compose", "up", "-d", name])
        elif action == "stop":
            return run_command(["docker", "compose", "stop", name])
        elif action == "restart":
            return run_command(["docker", "compose", "restart", name])
    elif BACKEND == "systemd":
        svc = f"zcode-proxy-{aid}.service"
        return run_command(["systemctl", "--user", action, svc])
    return 0, "ok"


# ------------------------------------------------------------- Live Quota Probing
def fetch_quota(i, timeout=8):
    port = i.get("port", 8080)
    api_key = i.get("apiKey", "")
    host = os.environ.get("ZCODE_PROXY_HOST", "127.0.0.1")
    url = f"http://{host}:{port}/quota"

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"state": "online", "data": data}
    except urllib.error.HTTPError as e:
        return {"state": "auth_error", "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"state": "offline", "detail": type(e).__name__}


def snapshot():
    global _snap_data, _snap_at
    now = time.time()
    with _snap_lock:
        if _snap_data and (now - _snap_at < SNAP_TTL):
            return _snap_data

    reg = load_reg()
    sup = load_supervisor_state()
    instances = reg.get("instances", [])

    with ThreadPoolExecutor(max_workers=min(10, len(instances) or 1)) as ex:
        quotas = list(ex.map(fetch_quota, instances))

    accounts = []
    totals = {}

    for i, q in zip(instances, quotas):
        online = q["state"] == "online"
        aid = i["id"]
        cstate = container_state(aid)

        entry = {
            "id": aid,
            "port": i.get("port", 8080),
            "state": q["state"],
            "detail": q.get("detail", ""),
            "balances": [],
            "signedIn": has_creds(aid),
            "container": cstate,
            "account": None
        }

        s_state = sup.get(aid, {})
        if s_state.get("state") == "asleep":
            entry["supervisor"] = {"state": "asleep", "wakeAt": s_state.get("wakeAt")}

        if online and "data" in q:
            d = q["data"]
            entry["account"] = d.get("account")
            entry["codingPlan"] = d.get("codingPlan")
            entry["plans"] = d.get("plans") or []
            entry["models"] = []
            try:
                host = os.environ.get("ZCODE_PROXY_HOST", "127.0.0.1")
                req = urllib.request.Request(
                    f"http://{host}:{i.get('port', 8080)}/v1/models?client_version=pi",
                    headers={"Authorization": "Bearer " + i.get("apiKey", "")},
                )
                with urllib.request.urlopen(req, timeout=4) as response:
                    catalog = json.load(response)
                entry["models"] = [{"id": m.get("slug"), "context": m.get("context_window"), "output": m.get("max_tokens")} for m in catalog.get("models", [])]
            except Exception:
                pass
            for b in d.get("balances", []):
                name = b.get("showName", "Unknown")
                used = b.get("usedUnits", 0)
                tot = b.get("totalUnits", 0)
                rem = b.get("remainingUnits", 0)
                expires = b.get("expiresAt")
                entry["balances"].append({
                    "name": name,
                    "used": used,
                    "total": tot,
                    "remaining": rem,
                    "expiresAt": expires,
                })
                totals[name] = totals.get(name, 0) + rem

        accounts.append(entry)

    res = {
        "accounts": accounts,
        "totals": totals,
        "serverTime": int(now),
        "backend": BACKEND,
        "jobs": jobs_public()
    }

    with _snap_lock:
        _snap_data = res
        _snap_at = now

    return res


# ------------------------------------------------------------- Async Worker Jobs
def jobs_public():
    with _lock:
        return [
            {
                "id": j["id"],
                "account": j["account"],
                "action": j["action"],
                "status": j["status"],
                "started": j["started"],
                "authUrl": j.get("authUrl", ""),
                "tail": j.get("tail", "")
            }
            for j in _jobs[:6]
        ]


def run_job(aid, action):
    jid = secrets.token_hex(4)
    ensure_dirs()
    log_path = LOGS_DIR / f"job-{aid}-{action}-{jid}.log"

    job = {
        "id": jid,
        "account": aid,
        "action": action,
        "status": "running",
        "started": time.time(),
        "tail": "",
        "log": str(log_path),
        "authUrl": ""
    }

    with _lock:
        _jobs.insert(0, job)
        del _jobs[20:]

    reg = load_reg()
    target_inst = inst(reg, aid)
    if not target_inst:
        job["status"] = "unknown_account"
        return

    try:
        if action in ("login", "login_start"):
            store_dir = STORES_DIR / f"account-{aid}"
            store_dir.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["ZCODE_PROXY_STORE_DIR"] = str(store_dir)
            env["ZCODE_PROXY_CREDENTIAL_SECRET"] = target_inst.get("credentialSecret", "")
            env["ZCODE_PROXY_API_KEY"] = target_inst.get("apiKey", "")
            env["ZCODE_PROXY_PORT"] = str(target_inst.get("port", 8080))

            cmd = [str(BUN_BIN), "run", "src/index.ts", "auth", "login", "zai"]

            proc = subprocess.Popen(
                cmd,
                cwd=str(APP_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            lines = []
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    lines.append(line)
                    log_path.write_text("".join(lines), encoding="utf-8")
                    job["tail"] = "".join(lines[-15:])
                    m = _URL_RE.search(line)
                    if m and not job["authUrl"] and "zcode.z.ai" in m.group(0):
                        job["authUrl"] = m.group(0)

            rc = proc.wait(timeout=1800)
            if rc != 0:
                job["status"] = f"failed(exit_{rc})"
            elif not has_creds(aid):
                job["status"] = "no_credentials_after_login"
            else:
                job["status"] = "done"
                control_service(aid, "restart")
        elif action == "start":
            rc, out = control_service(aid, "start")
            job["status"] = "done" if rc == 0 else f"failed({out})"
        elif action == "stop":
            rc, out = control_service(aid, "stop")
            job["status"] = "done" if rc == 0 else f"failed({out})"
        elif action == "refresh_quota":
            invalidate_snapshot()
            job["status"] = "done"
    except Exception as e:
        job["status"] = f"error({type(e).__name__})"
    finally:
        invalidate_snapshot()


# ------------------------------------------------------------- HTTP Handler
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZCode Proxy Dashboard</title>
  <style>
    :root {
      --bg: #0f1117;
      --card: #181b24;
      --border: #262b3a;
      --fg: #e2e8f0;
      --muted: #8892b0;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --radius: 10px;
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--fg);
      margin: 0;
      padding: 24px;
      line-height: 1.5;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    h1 { margin: 0; font-size: 22px; font-weight: 700; }
    .meta { font-size: 13px; color: var(--muted); margin-top: 4px; }
    .meta a { color: var(--primary); text-decoration: none; }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
    button {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--fg);
      padding: 8px 14px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    button:hover { background: #222736; border-color: #3b435b; }
    button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
    button.primary:hover { background: var(--primary-hover); }
    .totals { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin-bottom: 24px; }
    .tcard { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 18px; }
    .tcard .k { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
    .tcard .v { font-size: 22px; font-weight: 700; color: var(--success); margin-top: 4px; }
    .auth-banner {
      background: #0d2b1f;
      border: 1px solid #1c6b4b;
      border-radius: var(--radius);
      padding: 16px 20px;
      margin-bottom: 24px;
    }
    .auth-banner h3 { margin: 0 0 6px 0; font-size: 15px; color: #a7f3d0; }
    .auth-banner a.btn {
      display: inline-block;
      background: var(--success);
      color: #fff;
      font-weight: 600;
      text-decoration: none;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 13px;
      margin: 8px 0;
    }
    .auth-banner .url-box {
      font-family: monospace;
      font-size: 11px;
      color: #94a3b8;
      word-break: break-all;
      background: rgba(0,0,0,0.3);
      padding: 8px 12px;
      border-radius: 6px;
      margin-top: 6px;
    }
    table { width: 100%; border-collapse: collapse; margin-bottom: 20px; background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
    th, td { padding: 10px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid var(--border); }
    th { font-weight: 600; color: var(--muted); font-size: 11px; text-transform: uppercase; background: #13161f; }
    tr:last-child td { border-bottom: none; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge.online { background: #064e3b; color: #6ee7b7; border: 1px solid #047857; }
    .badge.stopped { background: #78350f; color: #fde68a; border: 1px solid #b45309; }
    .badge.offline { background: #7f1d1d; color: #fca5a5; border: 1px solid #b91c1c; }
    .badge.asleep { background: #1e1b4b; color: #c7d2fe; border: 1px solid #4338ca; }
    .bar-wrap { background: #262b3a; border-radius: 4px; height: 8px; width: 130px; display: inline-block; vertical-align: middle; margin-right: 8px; overflow: hidden; }
    .bar-fill { background: linear-gradient(90deg, #3b82f6, #10b981); height: 100%; border-radius: 4px; }
    .actions { display: flex; gap: 6px; justify-content: flex-end; }
    .actions button { padding: 4px 10px; font-size: 12px; }
    .notice { font-size: 13px; color: var(--warning); min-height: 20px; margin-bottom: 12px; }
    pre.jobs-log {
      background: #0b0d13;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-size: 11px;
      color: #94a3b8;
      max-height: 160px;
      overflow-y: auto;
      white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>ZCode Proxy Fleet</h1>
      <div class="meta">Status: <span id="ts">-</span> &middot; Backend: <span id="backend">-</span> &middot; Auto-refresh 15s</div>
    </div>
    <div class="toolbar">
      <button class="primary" onclick="addAccount()">+ Add Account</button>
      <button onclick="act('all', 'start')">Start All</button>
      <button class="primary" onclick="act('all', 'refresh_quota')">Refresh Quotas</button>
      <button onclick="act('all', 'stop')">Stop All</button>
    </div>
  </div>

  <div id="notice" class="notice"></div>
  <div id="auth-box"></div>
  <div class="totals" id="totals"></div>
  <div id="accounts-view"></div>

  <div id="jobs-section" style="margin-top: 24px;">
    <h3 style="font-size: 14px; margin-bottom: 8px; color: var(--muted); text-transform: uppercase;">Recent Background Tasks</h3>
    <pre class="jobs-log" id="jobs-log">No recent tasks</pre>
  </div>

  <script>
    const TOKEN = "__TOKEN__";
    let DATA = { accounts: [], totals: {} };

    function say(msg) { document.getElementById('notice').textContent = msg; }
    function esc(s) { return String(s || '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c])); }
    function fmt(n) {
      n = Number(n) || 0;
      const u = [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
      for (const [d, s] of u) { if (n >= d) return (n / d).toFixed(2).replace(/\.00$/, '') + s; }
      return String(n);
    }
    function ts(e) { if (!e) return '-'; return new Date(e * 1000).toLocaleString(); }

    async function act(aid, action) {
      say(`Executing ${action} for ${aid}...`);
      const r = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': TOKEN },
        body: JSON.stringify({ account: aid, action: action })
      });
      const res = await r.json();
      if (!r.ok) { say(`Error: ${res.error || r.statusText}`); }
      else { say(`Action ${action} triggered.`); setTimeout(load, 1500); }
    }

    async function addAccount() {
      const free = DATA.accounts.find(a => !a.signedIn && a.state !== 'online');
      if (!free) {
        say("All provisioned account slots are active or signed in.");
        return;
      }
      say(`Initiating OAuth sign-in for Account ${free.id}...`);
      await act(free.id, 'login_start');
    }

    async function load() {
      try {
        const r = await fetch('/api/state');
        DATA = await r.json();
        document.getElementById('ts').textContent = new Date(DATA.serverTime * 1000).toLocaleTimeString();
        document.getElementById('backend').textContent = DATA.backend || 'docker';

        // Totals cards
        let totHtml = '';
        for (const k in DATA.totals) {
          totHtml += `<div class="tcard"><div class="k">${esc(k)} Remaining</div><div class="v">${fmt(DATA.totals[k])}</div></div>`;
        }
        document.getElementById('totals').innerHTML = totHtml || '<div class="tcard"><div class="k">Quota</div><div class="v" style="color:var(--muted)">None</div></div>';

        // Auth banner
        const activeAuth = (DATA.jobs || []).find(j => j.authUrl && j.status === 'running');
        const authBox = document.getElementById('auth-box');
        if (activeAuth) {
          authBox.innerHTML = `
            <div class="auth-banner">
              <h3>Authorization Required for Account ${esc(activeAuth.account)}</h3>
              <div>Click the button below to approve sign-in in your browser:</div>
              <a class="btn" href="${esc(activeAuth.authUrl)}" target="_blank" rel="noopener">Open ZCode Login Page &rarr;</a>
              <div class="url-box">${esc(activeAuth.authUrl)}</div>
            </div>`;
        } else {
          authBox.innerHTML = '';
        }

        // Account tables
        let acctHtml = '';
        for (const a of DATA.accounts) {
          let badgeClass = 'offline';
          let badgeLabel = a.state;
          if (a.supervisor && a.supervisor.state === 'asleep') {
            badgeClass = 'asleep';
            badgeLabel = `Asleep (Wakes ${ts(a.supervisor.wakeAt)})`;
          } else if (a.state === 'online') {
            badgeClass = 'online';
            badgeLabel = 'Online';
          } else if (a.signedIn) {
            badgeClass = 'stopped';
            badgeLabel = 'Signed In (Stopped)';
          }

          const acctInfo = a.account ? `<span style="color:var(--muted)">(${esc(a.account.name || '')} ${esc(a.account.emailMasked || '')})</span>` : '';

          acctHtml += `
            <table>
              <tr>
                <th colspan="4" style="font-size:13px; color:var(--fg);">
                  Account ${esc(a.id)} ${acctInfo} &nbsp;&middot;&nbsp;
                  <span style="color:var(--muted)">Port ${esc(a.port)}</span> &nbsp;
                  <span class="badge ${badgeClass}">${esc(badgeLabel)}</span>
                </th>
                <th style="text-align:right;">
                  <div class="actions">
                    <button onclick="act('${a.id}', 'login')">Sign In</button>
                    <button onclick="act('${a.id}', 'start')">Start</button>
                    <button onclick="act('${a.id}', 'stop')">Stop</button>
                  </div>
                </th>
              </tr>`;

          if (a.state === 'online' && (a.balances.length > 0 || a.codingPlan)) {
            acctHtml += '<tr><th>Bucket</th><th>Used</th><th>Total</th><th>Remaining</th><th>Quota resets</th></tr>';
            if (a.codingPlan) {
              acctHtml += `<tr><td colspan="5"><strong>Coding plan: ${esc(a.codingPlan.level || 'Unknown')}</strong></td></tr>`;
              for (const l of a.codingPlan.limits || []) {
                const pct = l.percentage == null ? null : Number(l.percentage);
                const valid = pct !== null && Number.isFinite(pct);
                acctHtml += `<tr><td>${esc(l.type)}${l.total == null ? '' : ' · window ' + esc(l.total)}</td><td>${valid ? pct + '%' : 'Not supplied'}</td><td>100%</td><td>${valid ? Math.max(0, 100 - pct) + '%' : 'Not supplied'}${l.type === 'CREDIT_LIMIT' && l.remaining != null ? ' · ' + fmt(l.remaining) + ' credits' : ''}</td><td>${l.nextResetTime ? new Date(l.nextResetTime).toLocaleString() : 'Not supplied'}</td></tr>`;
              }
            }
            for (const b of a.balances) {
              const pct = b.total ? Math.min(100, Math.round(100 * b.used / b.total)) : 0;
              acctHtml += `
                <tr>
                  <td><strong>${esc(b.name)}</strong></td>
                  <td>${fmt(b.used)}</td>
                  <td>${fmt(b.total)}</td>
                  <td>
                    <div class="bar-wrap"><div class="bar-fill" style="width:${pct}%"></div></div>
                    ${fmt(b.remaining)}
                  </td>
                  <td style="color:var(--muted)">${ts(b.expiresAt)}</td>
                </tr>`;
            }
          } else {
            const reason = a.detail || (a.signedIn ? 'Service stopped' : 'Not signed in');
            acctHtml += `<tr><td colspan="5" style="color:var(--muted)">${esc(reason)}</td></tr>`;
          }
          for (const plan of a.plans || []) {
            acctHtml += `<tr><td colspan="5"><strong>Plan expires:</strong> ${plan.endsAt ? esc(ts(plan.endsAt)) : 'Not supplied'} · Started: ${plan.startsAt ? esc(ts(plan.startsAt)) : 'Not supplied'} · ${esc(plan.planId)}</td></tr>`;
          }
          if (a.models && a.models.length) {
            acctHtml += `<tr><td colspan="5"><details><summary>Model context / output limits (configured catalog; not measured)</summary><table><tr><th>Model</th><th>Context tokens</th><th>Max output tokens</th></tr>${a.models.map(m => `<tr><td>${esc(m.id)}</td><td>${fmt(m.context)}</td><td>${m.output == null ? 'Not supplied' : fmt(m.output)}</td></tr>`).join('')}</table></details></td></tr>`;
          }
          acctHtml += '</table>';
        }
        document.getElementById('accounts-view').innerHTML = acctHtml;

        // Jobs log
        const jobs = DATA.jobs || [];
        if (jobs.length > 0) {
          document.getElementById('jobs-log').textContent = jobs
            .map(j => `[${esc(j.account)}][${esc(j.action)}] status: ${esc(j.status)}\n${esc(j.tail || '')}`)
            .join('\n\n');
        }
      } catch (err) {
        console.error("Dashboard refresh error:", err);
      }
    }

    load();
    setInterval(load, 15000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, code, data, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")
        else:
            body = data.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = HTML_TEMPLATE.replace("__TOKEN__", token())
            self._send(200, html, content_type="text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/action":
            self._send(404, {"error": "not found"})
            return

        if self.headers.get("X-Auth-Token") != token():
            self._send(403, {"error": "unauthorized token"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._send(400, {"error": "invalid json payload"})
            return

        aid = str(payload.get("account", ""))
        action = str(payload.get("action", ""))

        if action not in ("start", "stop", "login", "login_start", "refresh_quota"):
            self._send(400, {"error": f"invalid action {action}"})
            return

        reg = load_reg()
        targets = [i["id"] for i in reg.get("instances", [])] if aid == "all" else [aid]

        for t in targets:
            threading.Thread(target=run_job, args=(t, action), daemon=True).start()

        invalidate_snapshot()
        self._send(200, {"ok": True, "action": action, "targets": targets})


def main():
    ensure_dirs()
    bind = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", sys.argv[1] if len(sys.argv) > 1 else 39777))

    server = ThreadingHTTPServer((bind, port), DashboardHandler)
    print(f"[ZCode Dashboard] Server started at http://{bind}:{port}/ (Backend: {BACKEND})", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ZCode Dashboard] Shutting down.")


if __name__ == "__main__":
    main()
