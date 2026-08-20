# Design: OAuth 2.1 for monarch-mcp as a Claude custom connector

Date: 2026-08-20
Status: approved design, not yet implemented
Scope: `server.py`, `docker-compose.yml`, `verify_server.sh`, `.env.example`,
`CLAUDE.md`, one new test file

## Goal

Make `https://mm-mcp.richardadonnell.com/mcp` addable in Claude via
**Customize > Connectors > Add custom connector**, so the 11 tools work on Claude
Desktop, web, and mobile — not only in Claude Code.

## Why OAuth is required

Claude's connector dialog does not accept a static `Authorization: Bearer` header.
The request for it (anthropics/claude-ai-mcp#112, opened 2026-03-22) was **closed as
not planned**. The `static_headers` auth type exists but is beta and gated behind
`mcp-review@anthropic.com`.

Supported auth types for remote connectors: `oauth_dcr`, `oauth_cimd`,
`oauth_anthropic_creds`, `custom_connection`, `static_headers` (beta), `none`.
`none` is disqualified — this server reads bank data.

That leaves OAuth. Claude requires **Dynamic Client Registration or CIMD**; it will
not use a hand-entered client_id/secret in the automated flow.

## Decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Authorization server | GitHub OAuth App via FastMCP `OAuthProxy` | Already have GitHub. No new SaaS holding a key to bank data. `OAuthProxy` presents a DCR interface to Claude while using one fixed upstream client, so GitHub's lack of DCR does not matter. |
| Existing `MCP_API_KEY` on `/mcp` | Keep working, via `MultiAuth` | n8n, Claude Code, and all 14 `verify_server.sh` assertions survive the change untouched. |
| OAuth state storage | Named volume at `FASTMCP_HOME=/data` | Default store is an encrypted file tree under `platformdirs` (`oauth_proxy/proxy.py:518-544`), which is ephemeral in a container. Without a volume, every Coolify redeploy wipes Claude's client registration and the refresh token. |
| Rollout | Straight to production | The change is backwards-compatible: `/api/*` is untouched and `/mcp` still accepts the old key. Only a boot failure or a wrong `base_url` can break it. |

### Explicitly accepted risk

`MultiAuth` keeping `MCP_API_KEY` valid on `/mcp` means OAuth **adds a door without
locking the old one**. A leaked `MCP_API_KEY` still reaches every tool. This is
deliberate — it buys a zero-breakage migration.

A second, unplanned reason to keep it emerged during client research:
**account-level connectors are known-broken in Claude Code mode**
(anthropics/claude-code#57158, opened 2026-05-08, **closed as not planned**).
OAuth connectors added in Desktop Settings return `403` / "Not connected" when
called from Claude Code mode while working normally in Claude chat. Anthropic
support attributed it to auth-state synchronization inside their connector proxy;
the MCP server logs **zero incoming requests**, so the failure never reaches this
codebase and cannot be fixed here.

The static-key path bypasses Anthropic's proxy entirely. It is therefore not just
a migration convenience but the **fallback for a live, unfixed client bug**. Do not
remove the `--header` entry from the Claude Code config after OAuth works.

Revisit deleting the `StaticTokenVerifier` only once #57158 is confirmed fixed and
nothing else depends on the key.

`StaticTokenVerifier`'s docstring says "Never use this in production — tokens are
stored in plain text". That warning is about hardcoding tokens. Here the token comes
from the `MCP_API_KEY` env var, which is already exactly what `APIKeyMiddleware`
compares against today. The change introduces no new exposure, but the deviation is
recorded here on purpose.

## Architecture

### Guard régimes after the change

| Path | Guard | Caller |
| --- | --- | --- |
| `/health` | none | Coolify healthcheck |
| `/api/*` | `APIKeyMiddleware` — `Bearer $MCP_API_KEY` | n8n |
| `/.well-known/*`, `/authorize`, `/token`, `/register`, `/auth/callback` | none, by protocol | Claude OAuth discovery |
| `/mcp` | `MultiAuth` — GitHub-issued FastMCP JWT **or** `$MCP_API_KEY` | Claude Desktop / web / mobile / Code |

### Why route mounting already works

`mcp.http_app()` calls `auth.get_routes(mcp_path="/mcp")` (`fastmcp/server/http.py:595`)
and `auth.get_middleware()` (`:592`), so OAuth endpoints and `.well-known` documents
are created inside the FastMCP ASGI app. Because `server.py` mounts that app at `/`
(`Mount("/", app=mcp_asgi)`), those routes land at the root — where RFC 9728 and
RFC 8414 require them. No manual hoisting of `get_well_known_routes()` is needed.

The PRM `resource` value is derived from `base_url` plus the MCP path, producing
`https://mm-mcp.richardadonnell.com/mcp`. It must match the URL typed into Claude
character for character.

## Components

### 1. `AllowlistedGitHubTokenVerifier` (new, ~15 lines)

Subclasses `fastmcp.server.auth.providers.github.GitHubTokenVerifier`.
Calls `super().verify_token(token)`; if that returns a token, compares
`claims["login"]` against `GITHUB_ALLOWED_USER` and returns `None` on mismatch.

Returning `None` fails closed and FastMCP renders it as `401` with a
`WWW-Authenticate` header — the exact handshake Claude needs.

**This is the single highest-severity component in the design.** Without it, any
GitHub account on earth that finds the URL completes the OAuth flow and reads the
Monarch account. It gets a dedicated test.

### 2. `OAuthProxy` construction (new, ~12 lines)

`GitHubProvider` builds its verifier internally (`providers/github.py:285`) and
exposes no injection point, so using it would require reaching into a private
attribute. `OAuthProxy` accepts `token_verifier=` as a public argument, and GitHub's
endpoints are constants:

- `upstream_authorization_endpoint`: `https://github.com/login/oauth/authorize`
- `upstream_token_endpoint`: `https://github.com/login/oauth/access_token`

Arguments to set: `upstream_client_id`, `upstream_client_secret`, `token_verifier`
(the allowlisted one), `base_url` (`PUBLIC_BASE_URL`), `redirect_path`
(`/auth/callback`), and `allowed_client_redirect_uris` restricted to
`https://claude.ai/api/mcp/auth_callback` plus `http://localhost:*` for Claude Code's
RFC 8252 loopback.

`jwt_signing_key` may be omitted — it is then derived from the upstream client secret
via PBKDF2 (`oauth_proxy/proxy.py:486-509`), which is stable as long as the GitHub
secret is. Because the storage directory is *also* derived from that key
(`:518-527`), rotating the GitHub client secret silently orphans the old store and
forces one reconnect. Acceptable; noted so it is not mistaken for a bug.

### 3. `MultiAuth` composition (~6 lines)

```python
MultiAuth(
    server=<the OAuthProxy>,
    verifiers=[
        StaticTokenVerifier({MCP_API_KEY: {"client_id": "legacy-api-key", "scopes": []}})
    ],
)
```

Passed as `FastMCP(..., auth=auth)`. Verification tries the proxy first, then the
static verifier. Routes and OAuth metadata come only from the proxy.

### 4. `APIKeyMiddleware` scoping (2-line change)

Currently applied globally (`server.py:857`) and guards every path except `/health`.
It must early-return for any path not starting with `/api/`, leaving `/mcp` and the
OAuth endpoints to FastMCP's own auth middleware. `/health` stays public exactly as
it is today.

### 5. Opt-in switch

If `GITHUB_CLIENT_ID` is unset, `auth=None` and the server behaves byte-identically
to today. This preserves the local-testing workflow (see gotcha #7 in `CLAUDE.md`)
and provides a one-variable rollback.

### Unchanged

- Lifespan delegation (`server.py:819-831`) — still required; gotcha #2 is unaffected.
- Route order — explicit routes before the catch-all `Mount`.
- All 11 tools and 5 prompts, and the Monarch API domain patch.

## Configuration

### New environment variables

| Variable | Required | Value |
| --- | --- | --- |
| `GITHUB_CLIENT_ID` | no — absence disables OAuth | from the GitHub OAuth App |
| `GITHUB_CLIENT_SECRET` | yes when `GITHUB_CLIENT_ID` set | from the GitHub OAuth App |
| `GITHUB_ALLOWED_USER` | yes when `GITHUB_CLIENT_ID` set | the one GitHub login permitted to connect |
| `PUBLIC_BASE_URL` | yes when `GITHUB_CLIENT_ID` set | `https://mm-mcp.richardadonnell.com` |
| `FASTMCP_HOME` | no, but required for persistence | `/data` |

`GITHUB_ALLOWED_USER` must be validated as non-empty at startup when OAuth is
enabled. An empty value must abort boot, never match-everyone.

`FASTMCP_HOME` unset still boots — it just relocates the store to an ephemeral
`platformdirs` path, which is failure mode 3 below.

`.env.example` gains all five variables with the same commented-block style it uses
for the Monarch auth pair. `CLAUDE.md` gains an env-var line and a gotcha covering
the `APIKeyMiddleware` scoping — the next person to add a route needs to know
`/api/*` and `/mcp` are now guarded by different mechanisms.

### GitHub OAuth App

Authorization callback URL: `https://mm-mcp.richardadonnell.com/auth/callback`.
Exact match; GitHub permits one. Default scope is `user`.

### docker-compose.yml

Add the five variables to the `environment:` block following the existing
`${VAR:-}` convention, a `volumes: - oauth-state:/data` entry on the service, and a
top-level `volumes: oauth-state:` stanza.

### Network

Anthropic's discovery, registration, and token requests originate from
`160.79.104.0/21`, with a **10-second** response budget (30s for refresh). Traefik
must not rate-limit, challenge, or geo-block that range, and must pass
`/.well-known/*` through to the app.

## Client setup

The connector is registered against the **Anthropic account**, not against any
machine. Anthropic's cloud makes the outbound call to the server, so nothing about
the local network matters on any client.

### Claude Desktop

`claude_desktop_config.json` is **not** used. That file is stdio-only; Desktop will
not connect to a remote URL configured there. The path is:

1. Claude Desktop, **Settings > Connectors** (on claude.ai web the same screen is
   **Customize > Connectors**)
2. **+**, then **Add custom connector**
3. URL: `https://mm-mcp.richardadonnell.com/mcp`
4. Leave **Advanced settings** empty. The OAuth Client ID/Secret fields there are for
   servers that do not support DCR; `OAuthProxy` does, so Claude registers itself.
5. Sign-in window opens, FastMCP consent screen, then GitHub consent
6. Per conversation, enable via the **+** button (or `/`), **Connectors**, toggle on

### Claude mobile (iOS and Android)

No steps. Adding the connector on Desktop or web is sufficient — it becomes available
on mobile at the next login on that account.

Two constraints on record: adding connectors *from* the mobile app is in beta, with
Desktop and web the documented primary path; and mobile cannot run local MCP at all.
The second is the entire justification for this design — remote OAuth is the only
transport that reaches a phone.

### Claude Code

No change required. The existing
`--header "Authorization: Bearer $MCP_API_KEY"` entry keeps working, and per the
accepted-risk section above it should be **kept** as the fallback for #57158.

To additionally use OAuth there:

```bash
claude mcp add --transport http monarch https://mm-mcp.richardadonnell.com/mcp --scope user
claude mcp login monarch      # or /mcp inside a session
```

Claude Code uses an RFC 8252 loopback redirect on a random port, which is why
`allowed_client_redirect_uris` includes `http://localhost:*`. `--callback-port` pins
it if a single exact port is preferred.

Note: if a configured `headers.Authorization` is rejected by the server, Claude Code
reports the connection as failed rather than falling back to OAuth. The two auth
paths do not chain on the client side.

### Known client-side bugs

Neither is fixable in this codebase; both are recorded so their symptoms are not
misdiagnosed as server faults.

- **anthropics/claude-code#57158** (2026-05-08, closed as not planned) — connectors
  return `403` / "Not connected" in Claude Code mode. Server receives no request.
  Mitigation: keep the static-key path, above.
- **anthropics/claude-ai-mcp#5** (2025-12-18, closed) — a Desktop update caused
  Claude to stop calling `/register`, `/authorize`, and `/token`, hitting only
  `/health`. Diagnostic: if Desktop reports a connection error while the logs show
  only health checks, the fault is client-side.

## Testing

### `verify_server.sh` — three new unauthenticated assertions (14 to 17)

Appended as sections 9, 10, 11 following the existing numbered-section style:

1. `GET /.well-known/oauth-protected-resource/mcp` with no credentials returns `200`,
   and its `resource` field equals `$BASE/mcp`.
2. `POST /mcp` with no credentials returns `401` **and** a `WWW-Authenticate` header
    containing `resource_metadata=`. A 401 without that header is the failure mode
    where Claude never shows a Connect prompt.
3. `GET /.well-known/oauth-authorization-server` returns `200` advertising
    `code_challenge_methods_supported: ["S256"]` and a `registration_endpoint`.

The existing 14 assertions must keep passing unmodified — that is the contract
`MultiAuth` was chosen to preserve. `EXPECTED_TOOLS` and `EXPECTED_PROMPTS` do not
change.

These three assertions must be skipped when OAuth is disabled, so
`verify_server.sh` stays usable against a server with no `GITHUB_CLIENT_ID`.

### `test_github_allowlist.py` (new)

Self-contained, no network, no Monarch account — same class as the existing two
`test_*.py` files. Stubs the parent `verify_token` and asserts:

- matching `login` returns the token
- non-matching `login` returns `None`
- parent returning `None` returns `None` (no crash on the missing-claims path)
- empty or unset `GITHUB_ALLOWED_USER` never matches

### Manual acceptance

Follows the Client setup section above.

1. `./verify_server.sh https://mm-mcp.richardadonnell.com` — 17 pass.
2. Claude Desktop, Settings > Connectors > Add custom connector,
   `https://mm-mcp.richardadonnell.com/mcp`, GitHub consent, tools appear.
3. Log in on Claude mobile; connector present with no configuration there, and a
   tool call returns live data.
4. A second GitHub account is refused.
5. Existing Claude Code `--header` entry still connects and calls a tool.
6. Redeploy, confirm the connector still works with no reconnect.

## Failure modes, ranked

1. **Allowlist wrong or bypassed — server open to every GitHub account.**
   Highest severity, lowest visibility. Mitigated by `test_github_allowlist.py`
   and manual acceptance step 4.
2. **`PUBLIC_BASE_URL` mismatch** — PRM `resource` disagrees with the typed URL,
   producing "Couldn't reach the MCP server". Diagnose by fetching the PRM and
   diffing `resource` against the URL entered in Claude.
3. **Volume not mounted** — silent; re-consent required after every deploy.
4. **`/.well-known/*` blocked by middleware or Traefik** — Claude never discovers
   the authorization server; the MCP server sees the request but GitHub sees no
   traffic at all.
5. **Boot failure from a missing variable** — healthcheck fails, Coolify holds the
   previous container.

Rollback: unset `GITHUB_CLIENT_ID` and redeploy. `auth=None`, behavior returns to
today's.

## Out of scope

- Connectors Directory submission
- Per-user identity to per-Monarch-account mapping (one account, one `mm` singleton)
- CIMD (`enable_cimd` exists on `OAuthProxy`; DCR is sufficient at one user)
- Removing `MCP_API_KEY` from `/mcp`
- The `.mcpb` desktop bundle (route 2 of the 2026-08-20 research)

## References

- Claude auth for connectors: <https://claude.com/docs/connectors/building/authentication>
- Custom connectors and request headers: <https://claude.com/docs/connectors/custom/remote-mcp>
- Lazy authentication, the 401 + PRM handshake: <https://claude.com/docs/connectors/building/lazy-authentication>
- Bearer header request, closed not planned: <https://github.com/anthropics/claude-ai-mcp/issues/112>
- FastMCP OAuth proxy: <https://gofastmcp.com/servers/auth/oauth-proxy>
- Use connectors, surface and sync behavior: <https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities>
- Get started with custom connectors: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>
- Claude Code MCP reference: <https://code.claude.com/docs/en/mcp>
- Connectors 403 in Claude Code mode, closed not planned: <https://github.com/anthropics/claude-code/issues/57158>
- Desktop OAuth regression, Dec 2025: <https://github.com/anthropics/claude-ai-mcp/issues/5>
