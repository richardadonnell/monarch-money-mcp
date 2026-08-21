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

```bash
./verify_server.sh http://localhost:8000          # 14 assertions (19 with OAuth on: +5 across 3 checks; skipped when OAuth is off)
./verify_server.sh https://mm-mcp.richardadonnell.com
python test_reauth.py                             # no network, no account needed
python test_update_transaction_kwargs.py          # no network, no account needed
python test_github_allowlist.py                   # no network, no account needed
```

No linter configured. The three `test_*.py` files are self-contained and safe to run
anywhere. `verify_server.sh` is NOT — it needs a live server and a real `MCP_API_KEY`,
and it makes live Monarch calls. That is why it is not named `test_*`: it must never be
picked up by a `test_*` glob sweep.

## Architecture

- `server.py` — everything: FastMCP tools, REST handlers, auth middleware,
  Starlette app assembly. ~990 lines, single module on purpose.
- `mm = MonarchMoney()` is a **module-level singleton**. `_init_monarch()`
  guards with `_monarch_ready` flag and is called lazily by every handler.
- Two Starlette layers: explicit `Route(...)` entries for `/api/*` + `/health`,
  then `Mount("/", mcp_asgi)` as catch-all for `/mcp`. **Order matters** —
  explicit routes must come before the mount or REST 404s.
- `APIKeyMiddleware` guards `/api/*` unconditionally, and also guards `/mcp`
  when OAuth is off. With OAuth on, `/mcp` belongs to FastMCP's own
  `RequireAuthMiddleware`, which accepts the same key via `MultiAuth`; the
  OAuth endpoints themselves stay public by protocol. `/health` is always
  public — see gotcha 9.

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

7. **A fake `MONARCH_TOKEN` is NOT isolation.** Running `server.py` locally
   reaches the LIVE Monarch account. When a bogus token 401s, `_reauth`
   (`server.py:279-320`) falls back to `MONARCH_EMAIL` / `MONARCH_PASSWORD` /
   `MONARCH_MFA_SECRET` from the environment, mints a real TOTP, logs in for
   real, and retries against production data. Demonstrated, not theoretical.
   Scrub those three vars before local testing — do not just fake the token.

8. **Pin FastMCP exactly.** `requirements.txt` says `fastmcp==4.0.0b3`. It used
   to be `fastmcp>=2.0`, which silently floated across major versions on every
   rebuild. 4.x serves MCP spec revision `2026-07-28` (the stateless protocol)
   and the older session-based handshake at the same time. Era routing is
   header-based in the `mcp` SDK: if the `MCP-Protocol-Version` request header
   is present and is not one of the legacy handshake versions, the request goes
   to the modern stateless handler; otherwise the legacy path runs. No
   server-side config flag selects this. Do not unpin — a major bump changes
   protocol behavior, not just the API surface.

9. **Two auth régimes, and the split is conditional.** `/api/*` is guarded by
   `APIKeyMiddleware`; `/mcp` is guarded by FastMCP's own `RequireAuthMiddleware`
   via `MultiAuth`, which accepts the same `MCP_API_KEY`. The OAuth endpoints
   (`/authorize`, `/token`, `/register`, `/consent`, `/auth/callback`,
   `/.well-known/*`) are **not** guarded by that middleware — they are
   deliberately public, as the OAuth protocol requires, and `APIKeyMiddleware`
   waves them through. But the `/mcp` split only applies when OAuth is on. When
   `GITHUB_CLIENT_ID` is unset,
   `_AUTH is None`, FastMCP installs no auth middleware at all, and
   `APIKeyMiddleware` must keep guarding `/mcp` itself. Narrowing it to `/api/*`
   unconditionally would leave `/mcp` open on the rollback path. See
   `APIKeyMiddleware.dispatch` in `server.py` (cited by symbol, not line — the
   line numbers in gotchas 2 and 3 above have already drifted). New routes:
   anything under `/api/` is covered automatically, anything else is not.

## Adding a new MCP tool

Pattern (mirror existing tools):
1. `@mcp.tool(name="...", annotations={"readOnlyHint": True/False, ...})`
   on an async function returning `_json(...)`.
2. Call `await _init_monarch()` before any `mm.*` call.
3. Build `kwargs` dict, only insert non-None values (see `get_transactions`).
4. If the tool mutates data, also add a `/api/...` REST handler in the
   routes list at `server.py:758-773`.
5. Bump `EXPECTED_TOOLS` in `verify_server.sh` (defaults to 11, one per
   `@mcp.tool`). Its exact-count check fails by design otherwise; it is
   env-overridable via `EXPECTED_TOOLS=12 ./verify_server.sh ...`.

## Adding a new MCP prompt

Prompts are the slash-command surface: Claude Code lists them as
`/mcp__monarch-money__<name>`. Claude Desktop has no prompt picker as of
Aug 2026, so a prompt is Claude Code / MCP-client only -- put anything Desktop
must see in a tool instead.

1. `@mcp.prompt(name="...")` on a plain (non-async) function returning `str`.
2. **Type every parameter `str`.** MCP sends prompt arguments as strings;
   interpolate them into the message text rather than relying on coercion.
   Give each one a default so the prompt is invocable bare.
3. No `_init_monarch()`, no `mm.*` call -- a prompt returns text and the model
   picks the tools. That is why `prompts/get` in `verify_server.sh` passes even
   when Monarch is down.
4. Bump `EXPECTED_PROMPTS` in `verify_server.sh` (defaults to 5, one per
   `@mcp.prompt`); env-overridable the same way as `EXPECTED_TOOLS`.

## Env vars

Required: `MCP_API_KEY`. Auth: either `MONARCH_TOKEN` (preferred, stateless)
OR `MONARCH_EMAIL` + `MONARCH_PASSWORD` (+ `MONARCH_MFA_SECRET` if 2FA).

OAuth (all optional; only needed to add this server as a Claude connector):
`GITHUB_CLIENT_ID` acts as the on/off switch — when it is set,
`GITHUB_CLIENT_SECRET`, `GITHUB_ALLOWED_USER`, and `PUBLIC_BASE_URL` become
required and the server refuses to boot without them. `FASTMCP_HOME=/data`
points OAuth state at the mounted volume.

Full reference in `.env.example`. README covers user-facing setup.

## Deployment

Coolify reads `environment:` block in `docker-compose.yml` — no `.env`
needed server-side. `MCP_API_KEY=${MCP_API_KEY:?...}` blocks deploy if unset.
Traefik handles HTTPS.
