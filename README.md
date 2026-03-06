# monarch-money-mcp

A portable, Docker-based MCP server for [Monarch Money](https://www.monarchmoney.com/) that speaks two protocols:

| Endpoint | Protocol | Consumer |
| --- | --- | --- |
| `/mcp` | MCP streamable-HTTP (FastMCP) | Claude Desktop / Allen |
| `/api/*` | Plain REST (JSON) | n8n HTTP Request nodes |
| `/health` | HTTP GET (no auth) | Docker / Coolify healthcheck |

**Stateless container** — Monarch session token lives in an env var, no filesystem state.

---

## Quick Start

```bash
cp .env.example .env
# Fill in MCP_API_KEY and either MONARCH_TOKEN or MONARCH_EMAIL + MONARCH_PASSWORD
docker compose up -d

# Verify
curl http://localhost:8000/health

# Check accounts (replace YOUR_API_KEY)
curl -H "Authorization: Bearer YOUR_API_KEY" http://localhost:8000/api/accounts
```

### Bootstrapping MONARCH_TOKEN

Run with email/password first, then retrieve the token so you can switch to stateless mode:

```bash
# 1. Start with email/password
MONARCH_EMAIL=you@example.com MONARCH_PASSWORD=secret docker compose up -d

# 2. Fetch the token
curl -H "Authorization: Bearer YOUR_API_KEY" http://localhost:8000/api/token

# 3. Paste the token into .env as MONARCH_TOKEN=, clear email/password, restart
docker compose restart
```

---

## Environment Variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `MCP_API_KEY` | Yes | Protects all endpoints |
| `MONARCH_TOKEN` | One of these | Monarch bearer token (stateless, preferred) |
| `MONARCH_EMAIL` | One of these | Monarch login email |
| `MONARCH_PASSWORD` | One of these | Monarch login password |
| `PORT` | No | Server port (default `8000`) |

---

## MCP Tools (for Claude Desktop)

| Tool | Description |
| --- | --- |
| `get_accounts` | All accounts with balances |
| `get_transactions` | Transactions with rich filtering |
| `get_cashflow_summary` | Income / expenses / savings totals |
| `get_cashflow` | Cashflow breakdown by category |
| `get_budgets` | Budget vs actual by category |
| `get_recurring_transactions` | Subscriptions and recurring bills |
| `get_account_holdings` | Investment account holdings |
| `get_net_worth_history` | Historical net worth snapshots |
| `get_transaction_categories` | All categories with IDs |
| `update_transaction` | Update category / notes / flags |
| `set_budget_amount` | Set monthly budget for a category |

---

## REST Endpoints (for n8n)

All require `Authorization: Bearer YOUR_API_KEY`.

```
GET  /api/accounts
GET  /api/transactions?limit=100&start_date=2026-03-01&end_date=2026-03-31
GET  /api/cashflow?start_date=2026-03-01&end_date=2026-03-31
GET  /api/budgets?start_date=2026-03-01&end_date=2026-03-31
GET  /api/recurring?start_date=2026-03-01&end_date=2026-03-31
GET  /api/networth?start_date=2026-01-01
POST /api/transaction/{id}   body: {"category_id": "...", "notes": "..."}
GET  /api/token              (returns current session token)
```

---

## Deployment on Coolify

1. Push this repo to GitHub (private)
2. Create a new **Docker Compose** app in Coolify pointing at this repo
3. Set all env vars in Coolify's Secrets UI
4. Enable HTTPS via Traefik — set domain `monarch-mcp.yourdomain.com`
5. Coolify exposes port 8000 through Traefik automatically

---

## Claude Desktop Config

Add to `claude_desktop_config.json`:

```json
"monarch-money": {
  "type": "streamable-http",
  "url": "https://monarch-mcp.yourdomain.com/mcp",
  "headers": {
    "Authorization": "Bearer YOUR_API_KEY"
  }
}
```
