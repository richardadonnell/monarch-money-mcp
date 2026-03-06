# Monarch Money MCP Server

A portable Docker-based server that exposes [Monarch Money](https://www.monarchmoney.com) personal finance data via two protocols simultaneously:

- **`/mcp`** — FastMCP streamable-HTTP for AI assistants that support the MCP protocol (Claude Desktop, etc.)
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

You need **either** `MONARCH_TOKEN` **or** both `MONARCH_EMAIL` + `MONARCH_PASSWORD`. Token is preferred — it's faster and stateless.

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

## Claude Desktop Config

Add this to your `claude_desktop_config.json`. Since Claude Desktop requires stdio-based MCP entries, use [`mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) as a bridge:

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
