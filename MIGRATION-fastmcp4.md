# Migration: FastMCP 3.4.7 → 4.x (MCP spec 2026-07-28)

Working checklist. Delete this file once Phase 3 is done and deployed.

## Context

The MCP spec revision `2026-07-28` (released 2026-07-28) made the protocol stateless:
no `initialize` handshake, no `Mcp-Session-Id`, new required `server/discover` RPC,
new `resultType` field on every result, new `Mcp-Method` / `Mcp-Name` request headers.

FastMCP 4.0.0b1+ supports it. Latest is `4.0.0b3` (2026-08-14). **4.0 stable not yet released.**

Era routing is header-based, in `mcp/server/streamable_http_manager.py:181-187`: if the
`MCP-Protocol-Version` header is present and is not one of the old handshake versions,
the request is dispatched to `handle_modern_request`. Otherwise the legacy handshake path
runs. One server serves both eras, negotiated per request. No config flag needed.

## Gap analysis — verified 2026-08-20

Grepped `server.py` for every API removed in FastMCP v4. **Zero hits.**

| v4 removes | Used here? |
|---|---|
| `httpx` → `httpx2` | No — monarchmoney uses gql/aiohttp |
| `McpError(ErrorData(...))` → `McpError(code=, message=)` | No |
| `ctx.sample()` / `ctx.list_roots()` / `ctx.elicit()` | No — no tool takes a `Context` param |
| `import_server`, `as_proxy`, `mount(prefix=)`, `add_tool_transformation` | No |
| `serializer=`, `exclude_args=` on `@mcp.tool` | No |
| resources / prompts / OpenAPI provider | No — tools only |
| `FastMCP.get_tools()` (removed, undocumented) | No |

Total FastMCP surface in `server.py`: `FastMCP(name, instructions=)`, 11x
`@mcp.tool(name=, annotations=)`, and `mcp.http_app()`. All three unchanged in v4.
`http_app()` keeps an identical signature plus one new optional `session_idle_timeout` kwarg.

Dependency floors for v4, all already satisfied:

- `pydantic >= 2.12` — have 2.13.4
- `starlette >= 1.0.1` — have 1.6.0
- `mcp >= 2.0.0` — pulled transitively by fastmcp-slim
- Python `>= 3.10` — Dockerfile is `python:3.12-slim`

### Proven by smoke test, not just docs

Rebuilt the exact `server.py` assembly pattern (FastMCP → `http_app()` →
`Mount("/", ...)` under Starlette, `APIKeyMiddleware`, delegated lifespan) against
`fastmcp==4.0.0b3` in a throwaway venv, then probed over real HTTP:

```
server/discover -> 200  supportedVersions:["2026-07-28"], resultType:"complete",
                        ttlMs, cacheScope, _meta.io.modelcontextprotocol/serverInfo
tools/list      -> 200  cacheScope + resultType present
tools/call      -> 200  (with Mcp-Method + Mcp-Name headers)
Mcp-Session-Id in response headers: False
```

Wrong/missing `Mcp-Name` correctly returns `-32020 HeaderMismatch`.

**Limit of that test:** it exercised a synthetic 10-line replica of the assembly, not the
real `server.py`, and against a beta. It covered every code path that touches FastMCP.
It did NOT cover a live Monarch API call. Phase 2 step 6 is what closes that.

---

## Phase 0 — Pin current known-good state (do first, regardless)

`requirements.txt` currently says `fastmcp>=2.0`. That already floats to 3.4.7 on every
rebuild, and will silently jump to 4.x the day 4.0 goes stable. Pin it so there is a
rollback point.

- [x] `requirements.txt`: `fastmcp>=2.0` → `fastmcp==3.4.7`
- [x] `docker compose up -d --build`
- [x] `curl http://localhost:8000/health` → 200 (container reports `healthy`; image has fastmcp 3.4.7, mcp 1.29.0)
- [x] Commit on its own (do not bundle with Phase 1)

## Decision point — before Phase 1

Ship on `4.0.0b3` now, or wait for `4.0` stable?

Recommendation: **wait**, unless you specifically want Claude negotiating the new protocol
against this server today. A single-replica personal-finance server gains nothing
functional from the stateless era, and beta means a second bump later anyway. Phase 0
captures most of the value at none of the risk; Phase 1 is a one-line change available
any day.

If shipping now: it works, it was tested, and legacy clients keep working.

- [ ] Decided: `_______________` (beta now / wait for stable)

## Phase 1 — Upgrade

- [ ] `requirements.txt`: `fastmcp==3.4.7` → `fastmcp==4.0.0b3` (or `==4.0.0` when stable)
- [ ] `requirements.txt`: `starlette>=0.37` → `starlette>=1.0.1`
- [ ] No `server.py` changes expected — if any are needed, this checklist was wrong, stop and re-check

Exact `==` pinning makes pip install the pre-release without needing `--pre`.

## Phase 2 — Verify

- [ ] 1. `docker compose up -d --build`
- [ ] 2. `curl http://localhost:8000/health` → 200
- [ ] 3. `docker compose logs -f monarch-mcp` → no `StreamableHTTPSessionManager task group was not initialized`
- [ ] 4. Legacy path: reconnect the `monarch-money` MCP entry in Claude Code (`/mcp`), tools still list
- [ ] 5. Modern path — expect `supportedVersions:["2026-07-28"]`:

```bash
curl -sS -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: server/discover" \
  -d '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}'
```

- [ ] 6. **Live data check** (the step the smoke test could not cover): one real
      `tools/call` against Monarch, e.g. `get_accounts`, returns real balances
- [ ] 7. One authed REST route → confirms the `/api/*` layer is untouched:

```bash
curl -H "Authorization: Bearer $MCP_API_KEY" http://localhost:8000/api/accounts
```

## Phase 3 — Deploy + docs

- [ ] Push to Coolify, confirm healthcheck passes
- [ ] Re-verify Phase 2 steps 2, 5, 6 against `https://mm-mcp.richardadonnell.com`
- [ ] `CLAUDE.md`: add gotcha for the FastMCP version pin + era-routing behavior
- [ ] `CLAUDE.md`: fix stale `~545 lines` claim (`server.py` is 781 lines)
- [ ] Delete this file

## Phase 4 — Optional, later

Not required. Only if a need appears.

- [ ] `fastmcp[tasks]` + `TasksExtension()` — if any Monarch call gets slow enough to background
- [ ] `stateless_http=True` + per-request Monarch auth — only if multiple replicas are wanted

Note: `mm = MonarchMoney()` is a module-level singleton (process state, not protocol
session state). Protocol statelessness does not make this server horizontally scalable —
N replicas still means N separate Monarch logins. Pre-existing, not introduced by v4.

## Do not break

- The lifespan delegation in `server.py` (`async with mcp_asgi.lifespan(app)`) is still
  required in v4. Removing it was reproduced as a hard `RuntimeError` against 4.0.0b3.
- Route order: explicit `Route(...)` entries must stay before `Mount("/", mcp_asgi)`.
- `MonarchMoneyEndpoints.BASE_URL` patch to `https://api.monarch.com` stays until
  hammem/monarchmoney#184 lands.

## Sources

- <https://gofastmcp.com/updates>
- <https://raw.githubusercontent.com/PrefectHQ/fastmcp/main/docs/getting-started/upgrading/from-fastmcp-3.mdx>
- <https://raw.githubusercontent.com/PrefectHQ/fastmcp/main/docs/changelog.mdx>
- <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
- <https://blog.modelcontextprotocol.io/posts/2026-07-28/>
- <https://pypi.org/pypi/fastmcp/json>
- <https://pypi.org/pypi/mcp/json>
