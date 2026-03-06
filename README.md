# Monarch Money MCP Server

A portable Docker-based server that exposes [Monarch Money](https://www.monarchmoney.com) personal finance data via two protocols simultaneously:

- **`/mcp`** — FastMCP streamable-HTTP for AI assistants that support the MCP protocol (Claude Desktop, etc.)
- **`/api/*`** — Plain REST endpoints for automation tools like n8n, Zapier, or custom scripts
- **`/health`** — Unauthenticated health check

---

## Quick Start

```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up -d
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MCP_API_KEY` | ✅ Yes | Long random string protecting all endpoints |
| `MONARCH_TOKEN` | Either/Or | Monarch bearer token (stateless, preferred) |
| `MONARCH_EMAIL` | Either/Or | Email for login fallback |
| `MONARCH_PASSWORD` | Either/Or | Password for login fallback |
| `MONARCH_MFA_SECRET` | If 2FA enabled | TOTP Base32 secret key (see below) |
| `PORT` | No | Port to listen on (default: `8000`) |

### Auth Strategy

**Option A — Token (preferred):**
Set `MONARCH_TOKEN` to your current Monarch bearer token. The server passes it directly without making a login call. Tokens last a long time; refresh as needed by hitting `GET /api/token` after a fresh login.

**Option B — Email/Password:**
Set `MONARCH_EMAIL` + `MONARCH_PASSWORD`. The server logs in on startup and caches the session in memory.

---

## 2FA / TOTP Setup

If your Monarch account has two-factor authentication enabled, you must provide the **raw Base32 secret key** — NOT the 6-digit rotating code.

### Finding your TOTP secret

**From a password manager (e.g. 1Password):**
1. Open your Monarch login entry
2. Click **Edit**
3. Find the OTP field → **Copy Secret Key**
4. It looks like: `JBSWY3DPEHPK3PXP` (uppercase Base32, ~20–32 characters)

**From Monarch directly:**
1. Go to **Settings → Security → Two-factor authentication**
2. When re-enabling 2FA, the setup screen shows a "two-factor text code" — that's the raw seed

Set it in `.env`:
```
MONARCH_MFA_SECRET=JBSWY3DPEHPK3PXP
```

The server uses the [`monarchmoney`](https://github.com/hammem/monarchmoney) library, which auto-computes the 6-digit TOTP code from the secret key — no manual code entry needed.

---

## REST API Endpoints

All endpoints require: `Authorization: Bearer {MCP_API_KEY}`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check (no auth) |
| `GET` | `/api/accounts` | All accounts with balances |
| `GET` | `/api/transactions?limit=100&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Transactions |
| `GET` | `/api/cashflow?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Cashflow by category |
| `GET` | `/api/budgets?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Budget vs actual |
| `GET` | `/api/recurring` | Recurring transactions & subscriptions |
| `GET` | `/api/networth?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Net worth history |
| `POST` | `/api/transaction/{id}` | Update a transaction (JSON body) |
| `GET` | `/api/token` | Get current Monarch session token |

---

## MCP Tools

| Tool | Description |
|---|---|
| `get_accounts` | All accounts with balances |
| `get_transactions` | Transactions with rich filtering |
| `get_cashflow_summary` | Income / expenses / savings rate |
| `get_cashflow` | Cashflow breakdown by category |
| `get_budgets` | Budget vs actual spend |
| `get_recurring_transactions` | Subscriptions and recurring items |
| `get_account_holdings` | Investment account holdings |
| `get_net_worth_history` | Historical net worth snapshots |
| `get_transaction_categories` | Category list with IDs |
| `update_transaction` | Update category, notes, or flags |
| `set_budget_amount` | Set monthly budget for a category |

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

