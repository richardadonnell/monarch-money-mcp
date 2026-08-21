# Monarch Money MCP Server

A portable Docker-based server that exposes [Monarch Money](https://www.monarchmoney.com) personal finance data via two protocols simultaneously:

- **`/mcp`** — FastMCP streamable-HTTP for AI assistants that support the MCP protocol. With [OAuth enabled](#claude-connector-setup-oauth) it works as a custom connector in Claude Desktop, claude.ai, and the mobile apps.
- **`/api/*`** — Plain REST endpoints for automation tools like n8n, Zapier, or custom scripts
- **`/health`** — Unauthenticated health check

---

## Getting Started

### Step 1 — Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose installed
- A [Monarch Money](https://www.monarchmoney.com) account

### Step 2 — Generate your API key

This is the key that protects all endpoints. Generate one now and keep it handy:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output — you'll use it as `MCP_API_KEY` in the next step.

### Step 3 — Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill it in. There are two ways to authenticate with Monarch:

---

#### Option A — Email + Password (easiest to start)

If you don't have a Monarch token yet, just use your login credentials:

```env
MCP_API_KEY=your-generated-key-here
MONARCH_EMAIL=you@example.com
MONARCH_PASSWORD=your-monarch-password
```

If your account has **2FA enabled**, you also need to add `MONARCH_MFA_SECRET` — see the [2FA setup section](#2fa--totp-setup) below before continuing.

Start the server:

```bash
docker compose up -d
```

Then grab your token so you can switch to Option B (recommended for long-term use):

```bash
curl -H "Authorization: Bearer your-generated-key-here" http://localhost:8000/api/token
```

You'll get back:

```json
{ "token": "5de1575d9833c4eb..." }
```

Copy that token value and set it as `MONARCH_TOKEN` in your `.env` — then you can remove `MONARCH_EMAIL` and `MONARCH_PASSWORD`.

---

#### Option B — Token only (recommended for long-term / production)

Once you have your Monarch token (from Option A above, or extracted from your browser), set it directly:

```env
MCP_API_KEY=your-generated-key-here
MONARCH_TOKEN=5de1575d9833c4eb...
```

The server uses the token directly and never makes a login call — this is stateless and works perfectly across container restarts.

> **Where to find your token in a browser:** Open Monarch Money → DevTools (F12) → Application tab → Local Storage → look for a key containing `token`. Or: Network tab → any API request → copy the `Authorization: Token ...` header value.

---

### Step 4 — Start the server

```bash
docker compose up -d
```

### Step 5 — Verify it's working

```bash
# Health check (no auth needed)
curl http://localhost:8000/health

# Fetch your accounts (replace with your MCP_API_KEY)
curl -H "Authorization: Bearer your-generated-key-here" http://localhost:8000/api/accounts
```

You should see `{"status": "ok"}` and a JSON list of your accounts. If you do, you're good to go.

**Next:** connect it to Claude. [Claude Connector Setup](#claude-connector-setup-oauth) is the path most people want — it covers Claude Desktop, claude.ai, and mobile in one setup. If you only ever use this from Claude Desktop against a local server, the [local stdio bridge](#local-stdio-bridge-claude-desktop-only) is simpler.

---

## Claude Connector Setup (OAuth)

This is how you get the tools into **Claude Desktop, claude.ai, and the mobile apps** — one setup, all three surfaces.

Claude's "Add custom connector" dialog does not accept a static `Authorization: Bearer` header ([the request for it was closed as not planned](https://github.com/anthropics/claude-ai-mcp/issues/112)), so reaching those surfaces requires OAuth. This server ships an optional GitHub-backed OAuth flow that admits exactly one GitHub account: yours.

**Prerequisite:** the server must be reachable over public HTTPS. Anthropic's infrastructure — not your laptop — makes the outbound call, so `localhost` cannot work here. See [Coolify Deployment](#coolify-deployment) for one way to get there, or use any host that terminates TLS for you. For local-only use, skip to [Local stdio bridge](#local-stdio-bridge-claude-desktop-only) below.

### Step 1 — Create a GitHub OAuth App

Go to [github.com/settings/developers](https://github.com/settings/developers) → **New OAuth App**.

| Field | Value |
|---|---|
| Application name | anything (`monarch-mcp`) |
| Homepage URL | your server URL |
| **Authorization callback URL** | `https://your-host.example.com/auth/callback` |

The callback URL must match **exactly** — it is `PUBLIC_BASE_URL` + `/auth/callback`, and GitHub permits only one. Generate a client secret and keep it with the client ID.

### Step 2 — Set the environment variables

```bash
GITHUB_CLIENT_ID=Ov23li...
GITHUB_CLIENT_SECRET=...
GITHUB_ALLOWED_USER=your-github-username
PUBLIC_BASE_URL=https://your-host.example.com
FASTMCP_HOME=/data
```

> **`PUBLIC_BASE_URL` must not include `/mcp`.** Claude is given `${PUBLIC_BASE_URL}/mcp`, but this variable is the base. Include `/mcp` here and the entire OAuth surface relocates under `/mcp/...`, discovery 404s, and the connector fails with an unhelpful client-side error.

`FASTMCP_HOME=/data` points OAuth state at the named volume declared in `docker-compose.yml`. Without a persistent volume there, every redeploy wipes the registered client and forces you to re-authorize the connector by hand.

### Step 3 — Deploy and verify

Deploy with those variables set ([Coolify Deployment](#coolify-deployment) covers one setup), then:

```bash
EXPECT_OAUTH=1 ./verify_server.sh https://your-host.example.com
```

Expect **19/19**. The `EXPECT_OAUTH=1` flag matters: without it, a dead OAuth surface is reported as a *skip* and the run still exits `0`, so a broken deploy looks green. With it, the three OAuth checks fail loudly.

Three of those assertions are the ones worth reading if something goes wrong:

- `resource` in the protected-resource metadata must equal the URL you type into Claude, character for character.
- Unauthenticated `POST /mcp` must return `401` **with** a `WWW-Authenticate` header — that header is what makes Claude show a Connect button instead of an error.
- The authorization-server metadata must advertise S256 PKCE and a `registration_endpoint`, because Claude requires Dynamic Client Registration.

### Step 4 — Add the connector

In **Claude Desktop → Settings → Connectors** (on claude.ai it's **Customize → Connectors**):

1. **+** → **Add custom connector**
2. URL: `https://your-host.example.com/mcp` — **with** `/mcp` this time
3. Leave **Advanced settings** empty. Those OAuth Client ID/Secret fields are for servers that don't support Dynamic Client Registration; this one does, so Claude registers itself.
4. Approve the consent screen, then sign in with GitHub

Per conversation, enable it with the **+** button → **Connectors**.

**Mobile needs no setup.** The connector lives on your Anthropic account, so it appears on iOS and Android at your next login. (Adding connectors *from* mobile is in beta; Desktop and web are the supported path.)

### Notes

- **Only `GITHUB_ALLOWED_USER` gets in.** Any other GitHub account can complete the sign-in, then receives `401` on every tool call. Fail-closed on data.
- **`MCP_API_KEY` still works on `/mcp`.** OAuth adds a door without locking the old one, which keeps n8n and existing Claude Code configs working unchanged. It also means a leaked `MCP_API_KEY` still reaches every tool — treat it accordingly.
- **Keep the `--header` entry in your Claude Code config.** Account-level connectors are known-broken in Claude Code mode ([claude-code#57158](https://github.com/anthropics/claude-code/issues/57158), closed as not planned): they return `403` while working normally in Claude chat. The static-key path bypasses Anthropic's connector proxy entirely and is the reliable fallback.
- **`/register` is unauthenticated by protocol** and its client registrations are written without a TTL. If your server is publicly discoverable, consider a reverse-proxy rate limit on that path that excludes Anthropic's egress range `160.79.104.0/21`.

---

## Local stdio bridge (Claude Desktop only)

For a server running on `localhost`, or if you'd rather not set up OAuth. This path works only in Claude Desktop — it cannot reach claude.ai or mobile.

Add this to your `claude_desktop_config.json`. Since that file accepts only stdio-based MCP entries, use [`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) as a bridge:

```json
{
  "mcpServers": {
    "monarch-money": {
      "command": "uvx",
      "args": [
        "mcp-proxy",
        "--transport",
        "streamablehttp",
        "http://localhost:8000/mcp"
      ],
      "env": {
        "API_ACCESS_TOKEN": "YOUR_MCP_API_KEY"
      }
    }
  }
}
```

> `uvx` is bundled with [uv](https://github.com/astral-sh/uv). Install it with `pip install uv` or `brew install uv`.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MCP_API_KEY` | ✅ Yes | Protects all endpoints — generate with `secrets.token_hex(32)` |
| `MONARCH_TOKEN` | Either/Or | Monarch bearer token (stateless, preferred) |
| `MONARCH_EMAIL` | Either/Or | Email for login fallback |
| `MONARCH_PASSWORD` | Either/Or | Password for login fallback |
| `MONARCH_MFA_SECRET` | If 2FA enabled | TOTP Base32 secret key (see below) |
| `PORT` | No | Port to listen on (default: `8000`) |
| `GITHUB_CLIENT_ID` | No | Enables the Claude connector OAuth flow — see [Claude Connector Setup](#claude-connector-setup-oauth) |
| `GITHUB_CLIENT_SECRET` | If OAuth on | GitHub OAuth App client secret |
| `GITHUB_ALLOWED_USER` | If OAuth on | The **one** GitHub username allowed to connect |
| `PUBLIC_BASE_URL` | If OAuth on | Public HTTPS base URL, **without** `/mcp` |
| `FASTMCP_HOME` | If OAuth on | Where OAuth state persists (`/data`, mounted volume) |

You need **either** `MONARCH_TOKEN` **or** both `MONARCH_EMAIL` + `MONARCH_PASSWORD`. Token is preferred — it's faster and stateless.

`GITHUB_CLIENT_ID` is an on/off switch for OAuth. Leave it empty and the server behaves exactly as it did before OAuth existed. Set it and the other three become mandatory — the server refuses to boot without them rather than starting up half-configured.

### When a session expires

Monarch tokens go stale eventually. When a call comes back `401`, the server re-authenticates once with `MONARCH_EMAIL` / `MONARCH_PASSWORD` (+ auto-TOTP) and retries that call. Concurrent 401s share a single login, so a rotating TOTP code is never spent twice.

Set **all three** — `MONARCH_TOKEN` *and* the credentials — for the best of both: fast stateless startup, plus self-healing when the token dies. With a token but no credentials there is nothing to re-mint, so the `401` surfaces and you have to replace `MONARCH_TOKEN` by hand.

A second consecutive `401` is treated as a real auth failure (wrong password, revoked MFA) and surfaces rather than retrying.

If the re-login itself is rejected, the server backs off for 60s (`AUTH_COOLDOWN_SECONDS`) before attempting another one. Requests during the cooldown fail fast with the underlying `401` instead of each firing its own login — a stuck credential should not turn every incoming request into a login attempt against Monarch.

Reading the error tells you which layer broke: `HTTP Code 401: Unauthorized` means the *login* was rejected, while `401, message='Unauthorized', url='...graphql'` means the *session* expired and the retry is about to run.

---

## 2FA / TOTP Setup

If your Monarch account has two-factor authentication enabled, you must provide the **raw Base32 secret key** — NOT the 6-digit rotating code you type when logging in.

### Finding your TOTP secret

**From a password manager (e.g. 1Password, Bitwarden):**

1. Open your Monarch Money login entry
2. Click **Edit**
3. Find the OTP / Authenticator field → **Copy Secret Key**
4. It looks like: `JBSWY3DPEHPK3PXP` (uppercase letters and numbers, ~20–32 characters)

**From Monarch directly (requires disabling and re-enabling 2FA):**

1. Go to **Settings → Security → Two-factor authentication**
2. Click **Disable**, then re-enable it
3. On the setup screen, Monarch shows a "two-factor text code" — that's the raw seed

Add it to `.env`:

```env
MONARCH_MFA_SECRET=JBSWY3DPEHPK3PXP
```

The server uses the [`monarchmoney`](https://github.com/hammem/monarchmoney) library to auto-compute the 6-digit TOTP code from the secret — you never need to type the 6-digit code manually.

> **Important:** `MONARCH_MFA_SECRET` is only needed when using email/password login (Option A). If you're using `MONARCH_TOKEN` directly, 2FA is already baked into the token and this variable is not needed.

---

## REST API Endpoints

All endpoints require: `Authorization: Bearer {MCP_API_KEY}` except `/health`.

---

### `GET /health`

Unauthenticated health check.

```json
{ "status": "ok" }
```

---

### `GET /api/accounts`

All accounts with current balances, types, and metadata.

No query parameters.

---

### `GET /api/transactions`

Transactions with optional filtering.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `100` | Max transactions to return (max 500) |
| `start_date` | `YYYY-MM-DD` | — | Filter from this date inclusive |
| `end_date` | `YYYY-MM-DD` | — | Filter to this date inclusive |
| `search` | string | — | Free-text search across merchant/description |
| `category_ids` | string (comma-separated IDs) | — | Restrict to these category IDs |
| `account_ids` | string (comma-separated IDs) | — | Restrict to these account IDs |
| `tag_ids` | string (comma-separated IDs) | — | Restrict to these tag IDs |
| `has_attachments` | boolean | — | Filter by attachment presence |
| `has_notes` | boolean | — | Filter by note presence |
| `is_split` | boolean | — | Filter split transactions |
| `is_recurring` | boolean | — | Filter recurring transactions |

Example:

```
GET /api/transactions?limit=50&start_date=2026-03-01&end_date=2026-03-31&search=Netflix
```

---

### `GET /api/cashflow`

Detailed cashflow breakdown by category for a period.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Period start |
| `end_date` | `YYYY-MM-DD` | Period end |

---

### `GET /api/budgets`

Budget categories with planned amounts and actual spending.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Budget period start |
| `end_date` | `YYYY-MM-DD` | Budget period end |

---

### `GET /api/recurring`

Recurring transactions and subscriptions.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Period start |
| `end_date` | `YYYY-MM-DD` | Period end |

---

### `GET /api/networth`

Historical net worth snapshots (wraps `get_aggregate_snapshots`).

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | History start date |
| `end_date` | `YYYY-MM-DD` | History end date |

---

### `POST /api/transaction/{id}`

Update a transaction's category, notes, or flags.

**Path parameter:** `id` — the Monarch transaction ID

**JSON body** (all fields optional):

```json
{
  "category_id": "string",
  "notes": "string",
  "hide_from_reports": false,
  "needs_review": true
}
```

---

### `GET /api/token`

Return the current Monarch session token. Useful for bootstrapping `MONARCH_TOKEN` after an initial email/password login.

Requires auth. Returns:

```json
{ "token": "5de1575d..." }
```

---

## MCP Tools

---

### `get_accounts`

Return all Monarch Money accounts with current balances, types, and metadata.

No parameters.

---

### `get_transactions`

Return transactions with optional filtering.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `100` | Max transactions (max 500) |
| `start_date` | `YYYY-MM-DD` | — | Filter from this date inclusive |
| `end_date` | `YYYY-MM-DD` | — | Filter to this date inclusive |
| `search` | string | — | Free-text search across merchant/description |
| `category_ids` | list[string] | — | Restrict to these category IDs |
| `account_ids` | list[string] | — | Restrict to these account IDs |
| `tag_ids` | list[string] | — | Restrict to these tag IDs |
| `has_attachments` | boolean | — | Filter by attachment presence |
| `has_notes` | boolean | — | Filter by note presence |
| `is_split` | boolean | — | Filter split transactions only |
| `is_recurring` | boolean | — | Filter recurring transactions only |

---

### `get_cashflow_summary`

Return cashflow totals — income, expenses, and savings rate — for a period.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `start_date` | `YYYY-MM-DD` | First of current month | Period start |
| `end_date` | `YYYY-MM-DD` | Today | Period end |

---

### `get_cashflow`

Return detailed cashflow breakdown by category.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Period start |
| `end_date` | `YYYY-MM-DD` | Period end |

---

### `get_budgets`

Return budget categories with planned amounts and actual spending.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Budget period start |
| `end_date` | `YYYY-MM-DD` | Budget period end |

---

### `get_recurring_transactions`

Return recurring transactions and subscriptions.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | Period start |
| `end_date` | `YYYY-MM-DD` | Period end |

---

### `get_account_holdings`

Return current holdings for an investment account.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `account_id` | string | ✅ | Monarch account ID — get from `get_accounts` first |

---

### `get_net_worth_history`

Return historical net worth snapshots.

| Parameter | Type | Description |
|---|---|---|
| `start_date` | `YYYY-MM-DD` | History start date |
| `end_date` | `YYYY-MM-DD` | History end date |

---

### `get_transaction_categories`

Return all transaction categories with IDs. Use IDs with `get_transactions` filters or `set_budget_amount`.

No parameters.

---

### `update_transaction`

Update a transaction's category, notes, or flags. All fields except `transaction_id` are optional.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `transaction_id` | string | ✅ | The transaction ID to update |
| `category_id` | string | — | New category ID (from `get_transaction_categories`) |
| `notes` | string | — | Free-text notes / memo |
| `hide_from_reports` | boolean | — | Exclude from spending reports |
| `needs_review` | boolean | — | Flag as needing review |

---

### `set_budget_amount`

Set the monthly budget amount for a category.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `amount` | float | ✅ | Budget amount in USD |
| `category_id` | string | ✅ | Category ID (from `get_transaction_categories`) |
| `start_date` | `YYYY-MM-01` | ✅ | First day of the target month |

---

## n8n HTTP Request Config

- **Method**: GET (or POST for updates)
- **URL**: `http://your-host:8000/api/accounts`
- **Headers**: `Authorization: Bearer YOUR_MCP_API_KEY`
- **Response format**: JSON

---

## Generate a Secure API Key

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Coolify Deployment

This server is Coolify-ready. The `docker-compose.yml` uses an `environment:` block with `${VAR}` substitution — Coolify detects these and surfaces them in its Environment Variables UI. No `.env` file needed on the server.

1. Create a new Coolify resource → **Docker Compose**
2. Point it at this repository
3. Set your environment variables in the Coolify UI (`MCP_API_KEY` is required; others are optional with sensible defaults)
4. Deploy — Coolify's Traefik proxy handles HTTPS automatically

If you're enabling the [Claude connector](#claude-connector-setup-oauth), two extra things matter here:

- The compose file declares a named volume `oauth-state` mounted at `/data`. Coolify persists named volumes across deploys, which is what keeps the connector authorized through a rebuild.
- Anthropic reaches your server from `160.79.104.0/21` with a **10-second** budget on OAuth discovery, registration, and token calls. A WAF, rate limit, or geo-block in front of Traefik can break the connector while `/health` still looks perfectly fine.

---

## Acknowledgments

This project is built on top of [monarchmoney](https://github.com/hammem/monarchmoney) by [@hammem](https://github.com/hammem) — the Python client library that handles all Monarch Money API communication. None of this would be possible without that work.
