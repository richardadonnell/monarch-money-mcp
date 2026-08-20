# monarch-mcp

Dual-protocol server exposing Monarch Money personal-finance data:
`/mcp` (FastMCP streamable-HTTP) + `/api/*` (REST for n8n) + `/health`.
Single file: `server.py`. Deployed via Docker Compose (Coolify-ready).

## Commands

```bash
docker compose up -d --build           # build + run
docker compose logs -f monarch-mcp     # tail logs
docker compose down                    # stop
curl http://localhost:8000/health      # smoke test (no auth)
curl -H "Authorization: Bearer $MCP_API_KEY" http://localhost:8000/api/accounts
```

No test suite, no linter configured. Verify changes by running the container
and hitting `/health` + one authed `/api/*` route.

## Architecture

- `server.py` — everything: FastMCP tools, REST handlers, auth middleware,
  Starlette app assembly. ~780 lines, single module on purpose.
- `mm = MonarchMoney()` is a **module-level singleton**. `_init_monarch()`
  guards with `_monarch_ready` flag and is called lazily by every handler.
- Two Starlette layers: explicit `Route(...)` entries for `/api/*` + `/health`,
  then `Mount("/", mcp_asgi)` as catch-all for `/mcp`. **Order matters** —
  explicit routes must come before the mount or REST 404s.
- `APIKeyMiddleware` checks `Authorization: Bearer {MCP_API_KEY}` on
  everything except `/health`.

## Gotchas

1. **Monarch API domain patch (required).** `monarchmoney` lib still points
   at `api.monarchmoney.com` (dead). `server.py:64` overrides:
   `MonarchMoneyEndpoints.BASE_URL = "https://api.monarch.com"`.
   Do not remove until upstream PR lands (ref hammem/monarchmoney#184).

2. **FastMCP lifespan must be delegated.** The parent Starlette `lifespan`
   wraps `mcp_asgi.lifespan(app)` (see `server.py:742-753`). Removing this
   breaks `/mcp` — `StreamableHTTPSessionManager` task group never starts.
   Still required in FastMCP 4.x — removing it was reproduced as a hard
   `StreamableHTTPSessionManager task group was not initialized` RuntimeError
   against `4.0.0b3`.

3. **`MONARCH_MFA_SECRET` is the Base32 seed, NOT the 6-digit code.** Users
   paste the rotating code constantly. Error message at `server.py:243-248`
   catches it; keep that hint intact.

4. **Token auth bypasses login entirely.** When `MONARCH_TOKEN` set, no
   `mm.login()` runs — `_headers["Authorization"] = f"Token {TOKEN}"` is
   set directly. MFA env var is unused in this path.

5. **Healthcheck reads `PORT` at runtime** (`docker-compose.yml:22`). Compose
   port mapping uses `${PORT:-8000}:${PORT:-8000}` so external == internal.
   Mismatch was the bug in commit 3f0a523 — keep them aligned.

6. **Update tool kwarg.** `mm.update_transaction(transaction_id=..., ...)` —
   the SDK param is `transaction_id`, not `id` (see `server.py:596`; passing
   `id` raises "unexpected keyword argument", fixed in commit 943f9bd).

7. **Pin FastMCP exactly.** `requirements.txt` says `fastmcp==4.0.0b3`. It used
   to be `fastmcp>=2.0`, which silently floated across major versions on every
   rebuild. 4.x serves MCP spec revision `2026-07-28` (the stateless protocol)
   and the older session-based handshake at the same time. Era routing is
   header-based in the `mcp` SDK: if the `MCP-Protocol-Version` request header
   is present and is not one of the legacy handshake versions, the request goes
   to the modern stateless handler; otherwise the legacy path runs. No
   server-side config flag selects this. Do not unpin — a major bump changes
   protocol behavior, not just the API surface.

## Adding a new MCP tool

Pattern (mirror existing tools):
1. `@mcp.tool(name="...", annotations={"readOnlyHint": True/False, ...})`
   on an async function returning `_json(...)`.
2. Call `await _init_monarch()` before any `mm.*` call.
3. Build `kwargs` dict, only insert non-None values (see `get_transactions`).
4. If the tool mutates data, also add a `/api/...` REST handler in the
   routes list at `server.py:758-773`.

## Env vars

Required: `MCP_API_KEY`. Auth: either `MONARCH_TOKEN` (preferred, stateless)
OR `MONARCH_EMAIL` + `MONARCH_PASSWORD` (+ `MONARCH_MFA_SECRET` if 2FA).
Full reference in `.env.example`. README covers user-facing setup.

## Deployment

Coolify reads `environment:` block in `docker-compose.yml` — no `.env`
needed server-side. `MCP_API_KEY=${MCP_API_KEY:?...}` blocks deploy if unset.
Traefik handles HTTPS.
