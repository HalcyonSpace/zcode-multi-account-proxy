# Environment variables

All settings have working defaults. For a normal Docker setup you only need to set the
three **required** values in `.env`.

## How to set them

**Docker (recommended)** – copy the template and edit it:

```bash
cp .env.example .env
nano .env                    # or any editor
docker compose up -d         # re-run after every change
```

`docker compose` reads `.env` automatically and passes the values into the containers.

**Generate strong random values** for the secrets:

```bash
openssl rand -base64 32                                             # Linux / macOS
python -c "import secrets; print(secrets.token_urlsafe(32))"        # any OS with Python
```

**Without Docker** (running `bun run src/index.ts serve` directly):

```bash
export ZCODE_PROXY_API_KEY='...'               # Linux / macOS (bash/zsh)
$env:ZCODE_PROXY_API_KEY = '...'               # Windows PowerShell
```

**Multi-account setup:** `dashboard/generate_compose.py` writes a separate random API key
and credential secret per slot into `data/accounts.json` and `docker-compose.multi.yml`.
You do not set those by hand.

---

## Required (`.env`)

| Variable | Used for | Example / how to set |
| --- | --- | --- |
| `PROXY_API_KEY` | The key every client (SDK, coding tool, OmniRoute) must send as `Authorization: Bearer <key>`. Passed into the container as `ZCODE_PROXY_API_KEY`. | A long random string. **Change it** – the compose fallback `change-me` is not safe. |
| `CREDENTIAL_SECRET` | Encrypts the saved login (`stores/.../credentials.json`). Passed in as `ZCODE_PROXY_CREDENTIAL_SECRET`. | A long random string. **Keep it stable** – if you change it, the saved login can no longer be decrypted and you must sign in again. |
| `PROXY_PORT` | Host port the proxy is published on. | `8080` (default). Change if 8080 is taken. |

## Optional (`.env`)

| Variable | Default | Used for |
| --- | --- | --- |
| `DASHBOARD_PORT` | `39777` | Host port of the web dashboard. |

## Memory tuning (already set in the Docker image)

These are baked into the `Dockerfile` with low-memory defaults. Only override them if
you know you need to (add them under `environment:` in `docker-compose.yml`).

| Variable | Image default | Effect |
| --- | --- | --- |
| `CAPTCHA_SOLVER_MODE` | `child` | `child` solves captchas in a short-lived process so memory is returned to the OS. `inprocess` keeps the old behaviour (more memory). |
| `CAPTCHA_CHILD_BATCH` | `2` | Captcha tokens minted per child process (1–20). Higher = fewer process starts, slightly more memory per start. |
| `CAPTCHA_CHILD_CONCURRENCY` | `1` | How many solver child processes may run at once. |
| `CAPTCHA_CHILD_TIMEOUT_MS` | `90000` | Kill a stuck solver child after this many ms. |
| `BUN_JSC_useJIT` | `0` | `0` disables the JavaScript JIT for a smaller memory baseline. Set `1` for slightly more CPU speed. |
| `CAPTCHA_POOL_MAX` | `60` | Max captcha tokens kept ready. |
| `ZCODE_CAPTCHA_LOW_CPU` | on | Set `0` to allow more parallel solving on a strong CPU. |

## Internal (set by the image / compose – normally leave alone)

| Variable | Value in Docker | Used for |
| --- | --- | --- |
| `ZCODE_PROXY_PORT` | `8080` | Port the proxy listens on **inside** the container. |
| `ZCODE_PROXY_CONFIG` | `/data/config.yaml` | Config file path (created from `config.example.yaml` on first start). |
| `ZCODE_PROXY_STORE_DIR` | `/store` | Folder holding the encrypted login. Mapped to `./stores/account-NN/`. |

## Dashboard and supervisor

Set by `docker-compose.yml` for the dashboard container; set them yourself only when
running `dashboard/dashboard.py` or `dashboard/supervisor.py` outside Docker.

| Variable | Default | Used for |
| --- | --- | --- |
| `DASHBOARD_BIND` | `0.0.0.0` | Address the dashboard listens on. Use `127.0.0.1` to keep it local-only. |
| `DASHBOARD_PORT` | `39777` | Dashboard port. |
| `ZCODE_BACKEND` | `docker` | How accounts are started/stopped: `docker`, `systemd`, or `process`. |
| `ZCODE_PROXY_HOST` | `127.0.0.1` | Host the dashboard/supervisor uses to reach the proxies (`zcode-proxy` inside Compose). |
| `DATA_DIR` / `STORES_DIR` / `LOGS_DIR` | `./data`, `./stores`, `./logs` | Where account registry, logins and job logs live. |
| `ZCODE_DASH_TTL` | `15` | Seconds the dashboard caches quota data. |
| `ZCODE_BUN` / `ZCODE_APP_DIR` | `bun`, repo root | Only for the `systemd` / `process` backends: which `bun` binary and app folder to use for sign-in. |

## Network and debugging

| Variable | Used for |
| --- | --- |
| `HTTPS_PROXY` / `HTTP_PROXY` | Send the captcha solver's traffic through an outbound HTTP proxy. |
| `ZCODE_LOG_FORMAT` | Set `compact` for one-line request logs. |

## Keep secret

Never commit `.env`, `stores/`, `data/` or `config/` – they hold your keys and logins.
They are already in `.gitignore`.
