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

- [x] Decided **2026-08-20: ship the beta now.** User chose to merge and deploy 4.0.0b3
      rather than hold for 4.0 stable, after local verification came back fully green.
      Revisit when 4.0 stable ships: change the pin to `fastmcp==4.0.0` and redeploy.

## Phase 1 — Upgrade

- [x] `requirements.txt`: `fastmcp==3.4.7` → `fastmcp==4.0.0b3`
- [x] `requirements.txt`: `starlette>=0.37` → `starlette>=1.0.1`
- [x] **No `server.py` changes were needed.** Confirmed: the v4 image builds, boots, and
      passes its healthcheck against a byte-for-byte unmodified `server.py`.

Exact `==` pinning makes pip install the pre-release without needing `--pre`.

Resolved inside the v4 image: `fastmcp 4.0.0b3`, `fastmcp-slim 4.0.0b3`, `mcp 2.0.0`,
`mcp-types 2.0.0`, `httpx2 2.12.0`, `starlette 1.6.0`, `pydantic 2.13.4`.

## Phase 2 — Verify

Run as an A/B: 3.4.7 stayed up on `:8000` while 4.0.0b3 ran alongside on `:8001`
(`PORT=8001 docker compose -p monarch-mcp-v4 up -d --build`). Same compose file, so the
only variable is the FastMCP version.

- [x] 1. `docker compose up -d --build` — v4 image built, container reports `healthy`
- [x] 2. `curl http://localhost:8001/health` → 200 `{"status":"ok"}`
- [x] 3. Logs clean — zero matches for `task group was not initialized` / `Traceback` / `ERROR`
- [x] 4. Legacy path intact: `initialize` with NO `MCP-Protocol-Version` header → HTTP 200,
      `protocolVersion: 2025-06-18`, `Mcp-Session-Id` header issued. Both eras served at once.
- [x] 5. Modern path: `supportedVersions: ["2026-07-28"]`, `resultType: "complete"`,
      `serverInfo.version: 4.0.0b3`, `cacheScope: "private"`, and NO `Mcp-Session-Id` header
- [x] 5b. `tools/list` → all **11** tools present, `resultType` + `cacheScope` on the result
- [x] 6. **Live data**: modern `tools/call` → `get_accounts` returned **23 real accounts**,
      `isError: false`, `resultType: "complete"`
- [x] 7. REST untouched: `/api/accounts` authed → 200 with the same 23 accounts;
      unauthed → 401 (`APIKeyMiddleware` still guards the mounted app)

### Negative control (3.4.7 on `:8000`)

The identical modern probe against 3.4.7 returns
`400 {"code":-32600,"message":"Bad Request: Missing session ID"}` — proving the 200s above
come from the v4 upgrade and not from the probe being lenient.

### Corrected probe command

The original command in this file was WRONG — it omitted the `_meta` envelope, and the v4
server correctly rejects that with
`-32602 params._meta must be an object carrying the required ... envelope keys`.
Working version:

```bash
KEY=$(grep '^MCP_API_KEY=' .env | cut -d= -f2- | tr -d '
"')
META='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"probe","version":"0"}}'

curl -sS -X POST http://localhost:8001/mcp   -H "Authorization: Bearer $KEY"   -H "Content-Type: application/json"   -H "Accept: application/json, text/event-stream"   -H "MCP-Protocol-Version: 2026-07-28"   -H "Mcp-Method: server/discover"   -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"server/discover\",\"params\":{$META}}"
```

`tools/call` additionally requires an `Mcp-Name` header matching the body's `name`
parameter, or the server returns `-32020 HeaderMismatch`.

## Phase 3 — Deploy + docs

- [x] Merged `fix/rest-update-transaction-kwarg` → `main`, then `chore/fastmcp-4-migration` → `main`
- [x] Rebuilt from merged `main` and re-ran the full suite: **8/8 passed** (the two changes
      had only been verified separately before this)
- [x] Pushed `main` (`3584c53..6f51941`) — Coolify auto-deploy triggered
- [ ] Re-verify Phase 2 steps 2, 5, 6 against `https://mm-mcp.richardadonnell.com`
- [x] `CLAUDE.md`: add gotcha for the FastMCP version pin + era-routing behavior (now Gotcha 7)
- [x] `CLAUDE.md`: fix stale `~545 lines` claim (`server.py` is 781 lines)
- [x] `CLAUDE.md`: all `server.py:NNN` references re-verified against the real file
      (64 BASE_URL, 742-753 lifespan, 243-248 MFA hint, 596 kwarg, 758-773 routes)
- [x] `CLAUDE.md`: Gotcha 6 corrected — the SDK kwarg is `transaction_id`, NOT `id`.
      The old text had it backwards; commit 943f9bd (2026-04-04) is the fix it describes.
- [ ] Delete this file

## Found in passing — NOT part of this migration

**Live bug: `POST /api/transaction/{id}` is broken.** `server.py:718` calls
`_call(mm.update_transaction, id=txn_id, **body)`. The monarchmoney SDK signature is
`update_transaction(self, transaction_id: str, ...)` — verified by inspecting the
installed module inside the running container. Passing `id=` raises
`got an unexpected keyword argument 'id'`, so this endpoint returns HTTP 500 for every
request.

Commit 943f9bd fixed exactly this on the MCP tool path (`server.py:596` correctly uses
`transaction_id`) but missed the REST handler. Pre-existing; unrelated to FastMCP 4.
One-line fix: `id=txn_id` → `transaction_id=txn_id`.

**Fixed 2026-08-20** on its own branch off `main` (commit `10acbc1`), kept separate from
the migration. Added `test_update_transaction_kwargs.py`, a non-mutating self-check that
binds both call sites' kwargs against the real SDK signature. Verified against a live
container with a nonexistent transaction id: the `unexpected keyword argument` TypeError
is gone and the call now reaches Monarch's API. No real transaction was modified.

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
