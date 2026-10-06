# Using this proxy with OmniRoute

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) is an AI gateway that can route many
providers behind one endpoint. Each proxy account here is a standard **OpenAI-compatible**
endpoint, so you add it to OmniRoute like any other OpenAI-compatible provider.

You need, per account:

| Value | Where to find it |
| --- | --- |
| Base URL | `http://<proxy-host>:<port>/v1` (single account: port `8080`; multi-account slot *N*: `8080 + N - 1`) |
| API key | `PROXY_API_KEY` from `.env`, or the slot's `apiKey` in `data/accounts.json` |

Which host to use depends on where OmniRoute runs:

| OmniRoute runs… | Base URL host |
| --- | --- |
| On the same machine, not in Docker | `127.0.0.1` |
| In Docker on the same machine | `host.docker.internal` (add `extra_hosts: ["host.docker.internal:host-gateway"]` on Linux), or put both stacks on one Docker network and use the container name with the **internal** port, which is always `8080` (e.g. `http://zcode-proxy-02:8080/v1`, not the published `8081`) |
| On another machine | the **proxy machine's** LAN / VPN address. Do **not** expose the proxy port to the public internet. |

Before you start, set these environment variables in the shell your assistant uses
(values never go into the chat):

| Variable | What it is | Where to get it |
| --- | --- | --- |
| `OMNIROUTE_KEY` | OmniRoute **admin / management** API key – lets the assistant create providers | OmniRoute dashboard → API keys / settings |
| `OMNIROUTE_CLIENT_KEY` | A normal OmniRoute **client** key – used only for the final test request | OmniRoute dashboard → API keys |
| `PROXY_KEY_1`, `PROXY_KEY_2`, … | The proxy key of each account slot | `PROXY_API_KEY` in `.env` (one account), or each slot's `apiKey` in `data/accounts.json` |

```bash
export OMNIROUTE_KEY='...'         # Linux / macOS
export OMNIROUTE_CLIENT_KEY='...'
export PROXY_KEY_1='...'
# Windows PowerShell:  $env:OMNIROUTE_KEY = '...'
```

Check the proxy is reachable from where OmniRoute runs:

```bash
curl -s "http://PROXY_HOST:8080/v1/models" -H "Authorization: Bearer PROXY_API_KEY"   # replace both
```

---

## Setup – let an AI assistant do it

The easiest way is to let your coding assistant do it. Paste this into it (Claude Code, Cursor, OpenCode, …) on the machine
that can reach both services. Fill in the placeholders first and **never paste real keys
into a public chat**.

```text
Add my ZCode proxy accounts to OmniRoute as OpenAI-compatible providers.

OmniRoute URL: <OMNIROUTE_URL>
OmniRoute admin key: env var OMNIROUTE_KEY (management API; do not print it).
OmniRoute client key: env var OMNIROUTE_CLIENT_KEY (for the /v1 test request; do not print it).
Proxy accounts (one line each: prefix, base URL, key env var):
  zcode1, http://<proxy-host>:8080/v1, PROXY_KEY_1
  zcode2, http://<proxy-host>:8081/v1, PROXY_KEY_2

For each account:
1. Confirm GET <base URL>/models returns 200 with the key.
2. Create an openai-compatible provider node (apiType chat) with that prefix and base URL.
3. Add one apikey connection to the node using the key, active, priority 1.
4. Sync the node's models.
5. Send one tiny chat request to OmniRoute's /v1/chat/completions with the client key,
   model <prefix>/glm-5.3-flash, and confirm it returns real text (HTTP 200 alone is not enough).
Do not modify or delete any other existing provider or connection.
Report a table: prefix, node id, connection id, models synced, test result.
```

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `401` from the proxy | Wrong key, or the slot's key from `data/accounts.json` was mixed up with another slot |
| Connection refused / timeout | OmniRoute cannot reach the host/port (see the host table above) |
| Models sync but requests fail with an auth error | That account is not signed in yet – sign it in from the dashboard |
| `429` / quota errors | That account is rate-limited or out of quota. If the supervisor is running and **all** of its buckets are empty, it stops the account until renewal; otherwise retry later or let OmniRoute fall back to another slot |
