# Migration: FastMCP 3.4.7 → 4.x (MCP spec 2026-07-28)

**STATUS: COMPLETE — deployed to production 2026-08-20.**

Kept rather than deleted (the original plan said to delete it) because the deployed
version is a BETA. When `fastmcp==4.0.0` stable ships, this file is the record of what
was changed, what was verified, and how to re-verify. Delete it after that bump.

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
- [x] Re-verified against `https://mm-mcp.richardadonnell.com` — **8/8 passed**.
      Coolify picked up the push and redeployed in ~20s.
- [x] Claude Code's `monarch-money` MCP entry reconnects to prod: `✔ Connected`
- [x] Smoke-tested all 9 read-only tools against 4.0.0b3 with live data — all OK.
      The 2 mutating tools (`update_transaction`, `set_budget_amount`) were deliberately
      NOT called, to avoid modifying real financial records.
- [x] Local A/B containers torn down
- [x] `CLAUDE.md`: add gotcha for the FastMCP version pin + era-routing behavior (now Gotcha 7)
- [x] `CLAUDE.md`: fix stale `~545 lines` claim (`server.py` is 781 lines)
- [x] `CLAUDE.md`: all `server.py:NNN` references re-verified against the real file
      (64 BASE_URL, 742-753 lifespan, 243-248 MFA hint, 596 kwarg, 758-773 routes)
- [x] `CLAUDE.md`: Gotcha 6 corrected — the SDK kwarg is `transaction_id`, NOT `id`.
      The old text had it backwards; commit 943f9bd (2026-04-04) is the fix it describes.
- [ ] Delete this file — **deferred until the 4.0 stable bump** (see status note at top)

### Security check on v4's new surface

The `2026-07-28` era adds `subscriptions/listen`, a long-lived POST-response stream that
replaced the old HTTP GET endpoint. It is new code reached through `APIKeyMiddleware`, so
it was worth confirming it does not bypass auth. Verified against the production image:

```
subscriptions/listen, no auth header   -> 401   (middleware guards it)
subscriptions/listen, valid key        -> reaches the handler and validates params
bogus/method, valid key                -> -32601 Method not found
```

No auth bypass on the new path. `/health` remains the only exempt route.

### Production verification, 2026-08-20

```
PASS  /health 200
PASS  REST unauthed 401
PASS  REST authed 200
PASS  modern supportedVersions ["2026-07-28"]
PASS  tools/list -> 11 tools
PASS  live tools/call get_accounts (isError false, 23 accounts)
PASS  legacy initialize 200 (older clients still work)
PASS  REST update_transaction kwarg fix live
```

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

## Independent audit — verdict: upgrade is clean

An adversarial audit was run against the claim "the upgrade needs zero `server.py`
changes", tasked with refuting it. **Verdict: SURVIVES — no v4-attributable breakage.**

The strongest evidence: holding a FastMCP **3.4.7 client** constant and byte-diffing full
tool descriptors and call results against a **4.0.0b3 server**. Across all 11 tools the
only difference is an auto-generated `title` field ("Get Accounts"). `inputSchema`,
`outputSchema`, `annotations`, `content`, `structuredContent` and error text are
byte-identical. `resultType` / `ttlMs` / `cacheScope` carry server-side defaults that
older peers ignore. **Already-deployed older clients keep working.**

Also established: no route shadowing (`http_app()` exposes exactly `Route /mcp` in both
3.4.7 and 4.0.0b3); no auth bypass under raw-socket path-traversal probes
(`/mcp/../health`, `//health`, `/..%2fhealth` all fail closed with 401); no
deprecation/camelCase warnings; `test_reauth.py` passes unmodified under v4.

### Still unaudited

- Sustained/concurrent load through `BaseHTTPMiddleware` wrapping a streaming mount.
  Unchanged from v3, so not a regression, but untested in both.
- The success path of the two mutating tools (`update_transaction`, `set_budget_amount`),
  deliberately never called with a valid ID. Kwarg names are statically verified.
- `session_idle_timeout` (new in v4) and long-running/timeout behavior.

## Dev-safety trap — NOT a v4 issue, but important

**A fake `MONARCH_TOKEN` is not isolation.** Running `server.py` locally reaches the
LIVE Monarch account. When a bogus token 401s, `_reauth` (`server.py:279-320`) falls back
to `MONARCH_EMAIL` / `MONARCH_PASSWORD` / `MONARCH_MFA_SECRET` from the environment,
mints a real TOTP, logs in for real, and retries the original request against production
data.

This was demonstrated, not theorized: an audit run with `MONARCH_TOKEN=faketoken`
performed a genuine login and a real `updateTransaction` mutation attempt. Nothing was
modified only because the transaction ID did not exist.

Anyone testing this repo locally should scrub `MONARCH_EMAIL` / `MONARCH_PASSWORD` /
`MONARCH_MFA_SECRET` from the environment, not just set a fake token.

## Pre-existing REST fragility — NOT v4, not fixed

All five `**params` REST handlers splat raw query strings into SDK calls with no coercion
or allowlist, so they return 500 rather than 4xx on bad input:

- `?foo=1` (any unknown param) → `TypeError: unexpected keyword argument` → 500, on
  `api_transactions` (:668), `api_cashflow` (:681), `api_budgets` (:690),
  `api_recurring` (:699), `api_networth` (:708)
- `?limit=abc` → `ValueError` → 500
- `?has_notes=false` → the string `"false"` reaches a GraphQL Boolean filter → 500.
  Same for `has_attachments`, `is_split`, `is_recurring`.

The MCP tool path is unaffected — FastMCP coerces from the JSON schema, so tools receive
real booleans. This is why the REST layer is where the bugs are.

Audited for more `id`/`transaction_id`-class kwarg mismatches: **none found.** Every
kwarg at every REST handler and MCP tool call site was diffed against the real
monarchmoney 0.1.15 signatures. Two apparent type mismatches are false alarms —
`get_aggregate_snapshots(start_date: Optional[date])` has a stale annotation and actually
wants ISO strings, and `get_account_holdings(account_id: int)` takes the string ID
Monarch issues.

## Follow-up: when FastMCP 4.0 stable ships

1. `requirements.txt`: `fastmcp==4.0.0b3` → `fastmcp==4.0.0`
2. Rebuild locally, re-run the Phase 2 suite against `:8001`
3. Push; Coolify redeploys in ~20s
4. Re-run the suite against prod
5. Delete this file

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
