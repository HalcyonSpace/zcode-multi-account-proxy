# ZCode Multi-Account Proxy

A small, memory-lean gateway that turns ZCode / Z.ai **Start-Plan** quota into standard
**OpenAI** and **Anthropic** compatible endpoints, with a web dashboard for running
several accounts side by side.

> **Built on top of [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api).**
> The core proxy is their work; this project adds a lower-memory captcha path, a
> multi-account dashboard, a quota-aware auto-sleep supervisor, Docker packaging and
> OmniRoute setup docs. Free and non-commercial – all credit for the proxy goes upstream.

---

## Features

- **Standard APIs** – OpenAI `/v1/chat/completions`, Anthropic `/v1/messages` and
  Responses `/v1/responses` on one port.
- **Low memory** – captcha tokens are minted in a short-lived child process, so the
  DOM/JS heap used for solving is returned to the OS after every batch.
- **Multi-account dashboard** – sign in, start, stop and watch quota for every account
  from one web page (`http://localhost:39777`).
- **Auto-sleep supervisor** – stops an account when every quota bucket is empty and
  starts it again after the renewal time.
- **Docker first** – one `docker compose up -d` for a single account; a generator script
  for N accounts.

## Supported plans

| Plan | Supported | Notes |
| --- | --- | --- |
| **Start-Plan** (starter / promo quota) | Yes | Default (`plan: start-plan`). Uses the account's promo and daily buckets. |
| **Coding plan** | Yes | Set `plan: coding-plan` in `config/config.yaml`. |
| **Promo buckets** | Yes | Every bucket (e.g. GLM-5.3-Flash, GLM-5.3) shows separately in the dashboard and `/quota`. |
| **Claimable trial plans** | Yes, optional | The claim helper can pick up claimable trial / weekend plans (`claim` section of the config). |
| **Off-peak (idle) plan** | Yes, optional | Async off-peak channel (`async` section of the config). |

> **You need a Gmail account.** ZCode sign-in currently works only with Google / Gmail
> (`@gmail.com`) accounts, and a new account signed in this way gets the **free 5-day
> starter plan**. Other email providers are not supported.

---

## How it works

```
 AI clients (any OpenAI / Anthropic SDK or coding tool)
                 │
                 ▼
 ┌──────────────────────────────┐      ┌─────────────────────────┐
 │ zcode-proxy  (port 8080)     │◄─────│ dashboard  (port 39777) │
 │  • OAuth / JWT session       │/quota│  • sign-in links        │
 │  • request translation       │      │  • live quota           │
 │  • captcha child process     │      │  • start / stop         │
 └──────────────┬───────────────┘      └────────────┬────────────┘
                │                                   │
                ▼                                   ▼
        ZCode upstream API                 supervisor (auto-sleep)
```

---

## Quick start (Docker, one account)

```bash
git clone <this-repo-url> zcode-proxy
cd zcode-proxy
cp .env.example .env          # set PROXY_API_KEY and CREDENTIAL_SECRET (see docs/ENVIRONMENT.md)
docker compose up -d
```

### Sign in

1. Open `http://localhost:39777` and click **Sign in**.
2. Open the authorization link that appears and approve it with a **Gmail** account.
3. The proxy picks up the credentials and starts serving.

CLI alternative:

```bash
docker compose exec zcode-proxy bun run src/index.ts auth login zai
```

### Test it

```bash
export PROXY_API_KEY='the value you put in .env'
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"Hello"}]}'
```

---

## Running several accounts

```bash
python dashboard/generate_compose.py --slots 3     # writes docker-compose.multi.yml + data/accounts.json
docker compose -f docker-compose.multi.yml up -d
python dashboard/supervisor.py                      # optional auto-sleep loop
```

- Slot *N* listens on port `8080 + (N - 1)`.
- Every slot gets its own random API key and credential secret in `data/accounts.json`
  (git-ignored).
- Sign each slot in from the dashboard, ideally in a private browser window per account.

---

## Connecting clients

| Client type | Base URL | Key |
| --- | --- | --- |
| OpenAI SDK / Cursor / Continue / OpenCode | `http://localhost:8080/v1` | your `PROXY_API_KEY` |
| Anthropic SDK / Claude Code | `http://localhost:8080` | your `PROXY_API_KEY` |

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="YOUR_PROXY_API_KEY")
print(client.chat.completions.create(
    model="glm-5.3-flash",
    messages=[{"role": "user", "content": "Explain async Python."}],
).choices[0].message.content)
```

---

## Using with OmniRoute

Every account is a plain OpenAI-compatible endpoint, so it can be added to
[OmniRoute](https://github.com/diegosouzapw/OmniRoute) as a provider:

- **Base URL:** `http://<proxy-host>:<port>/v1` (`8080` for one account, `8080 + N - 1` for slot *N*)
- **API key:** your `PROXY_API_KEY` (or the slot's `apiKey` in `data/accounts.json`)
- **Type:** OpenAI-compatible, Chat Completions

Setup guide with a copy-paste prompt for your AI assistant (Claude Code, Cursor, OpenCode …):
**[docs/OMNIROUTE.md](docs/OMNIROUTE.md)**.

---

## Benchmarks

Measured on a 4-core Linux machine running five accounts at once, with live streaming
traffic on one of them. Your numbers will vary with hardware and load.

### Memory per proxy process (RSS)

```
in-process solver (before) ████████████████████████████████  ~780 MB, kept growing
child-process solver (now) ███                               ~66–96 MB, flat
```

| Process | RSS |
| --- | --- |
| Proxy, account 1 (streaming) | 73.7 MB |
| Proxy, account 2 | 66.3 MB |
| Proxy, account 3 | 96.4 MB |
| Proxy, account 4 | 74.2 MB |
| Proxy, account 5 | 66.0 MB |
| Shared captcha helper | 390.8 MB |
| Dashboard | 35.8 MB |
| Supervisor | 30.1 MB |
| **Whole stack** | **~833 MB** |

### Load and speed

| Metric | Value |
| --- | --- |
| CPU per proxy | 1.7 – 2.4 % |
| Machine load average | 0.55 on 4 cores |
| Time to first byte | 1.5 – 4.4 s |
| Streaming speed (glm-5.3-flash) | 14 – 45 tokens/s |

### How many accounts can one machine run?

Estimated from the measurements above. Only 5 accounts were actually measured; larger
numbers are extrapolated, so leave headroom.

Budget per account: **~200 MB RAM** (proxy at its ~96 MB peak plus a ~100 MB captcha child
while it solves) and **~0.11 CPU cores** under live use (5 accounts gave a 0.55 load average).
Fixed cost: **~1 GB** for the OS and Docker, plus **~70 MB** for the dashboard and supervisor.

```
accounts ≈ min( (RAM_GB − 1.1) / 0.2 ,  CPU_cores × 8 )
```

| Machine | RAM limit | CPU limit | **Recommended max** |
| --- | --- | --- | --- |
| 1 vCPU / 2 GB | 4 | 8 | **4 accounts** |
| 2 vCPU / 4 GB | 14 | 16 | **14 accounts** |
| 4 cores / 8 GB | 34 | 32 | **32 accounts** |
| 4 cores / 16 GB | 74 | 32 | **32 accounts** |
| 8 cores / 16 GB | 74 | 64 | **64 accounts** |

- Accounts parked by the supervisor (quota empty) use almost no RAM or CPU, so you can
  register more slots than this, as long as only that many are awake at once.
- These limits cover your hardware only. The upstream service may rate-limit many
  accounts coming from one IP; that was not measured.

### Why memory stays flat

1. **Child-process captcha** (`CAPTCHA_SOLVER_MODE=child`) – the solver runs in a
   short-lived process; when it exits, all of its memory goes back to the OS.
2. **No JIT** (`BUN_JSC_useJIT=0`) – smaller baseline for an I/O-bound proxy.
3. **Smol mode** (`bun --smol`) – more frequent garbage collection.

---

## Configuration

All environment variables (what each does and how to set it): **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)**.

`config.example.yaml` is copied to `config/config.yaml` on first start.

| Key | Default | Meaning |
| --- | --- | --- |
| `server.port` | `8080` | Proxy port |
| `auth.proxyApiKey` | placeholder | Key clients must send |
| `plan` | `start-plan` | Quota tier |
| `defaultModel` | `glm-5.3-flash` | Used when a request has no model |
| `identity.deviceMid` | generated | Random device id, created on first login |

---

## Security

- Credentials are stored encrypted under `./stores/` and never leave your machine.
- `stores/`, `data/`, `config/`, `logs/` and `.env` are git-ignored – never commit them.
- Change the default `PROXY_API_KEY`, and keep ports `8080+` and `39777` on localhost or a private
  network. If you must reach them remotely, use a VPN/SSH tunnel or a TLS reverse proxy – the
  API key is sent in plain HTTP otherwise, and the dashboard can start/stop accounts.

## Credits

- Core proxy: [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api) – this
  repository is built on top of it.
- Gateway integration target: [OmniRoute](https://github.com/diegosouzapw/OmniRoute).

## Disclaimer

Free, non-commercial project. Unofficial, not affiliated with ZCode, Z.ai or Zhipu. Use only with accounts
you own and within the provider's terms of service.
