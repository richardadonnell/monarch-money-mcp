# OAuth Custom Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `https://mm-mcp.richardadonnell.com/mcp` addable as a Claude custom connector so the 11 tools reach Claude Desktop, web, and mobile.

**Architecture:** A GitHub OAuth App fronted by FastMCP's `OAuthProxy`, which presents a Dynamic Client Registration interface to Claude while using one fixed upstream client. A subclassed token verifier narrows access to a single GitHub login. `MultiAuth` keeps the existing `MCP_API_KEY` valid on `/mcp` so n8n, Claude Code, and all 14 existing `verify_server.sh` assertions keep working unchanged. OAuth state persists to a named Docker volume.

**Tech Stack:** Python 3.12, FastMCP 4.0.0b3 (pinned), Starlette, Docker Compose, bash + curl + jq for verification.

**Spec:** `docs/superpowers/specs/2026-08-20-oauth-connector-design.md`

## Global Constraints

- **No new dependencies.** Every import used here already ships inside `fastmcp==4.0.0b3`. `requirements.txt` must not change.
- **Do not unpin FastMCP.** `fastmcp==4.0.0b3` exactly (CLAUDE.md gotcha 8).
- **Do not remove the lifespan delegation** at `server.py:819-831` (CLAUDE.md gotcha 2). Removing it breaks `/mcp` with `StreamableHTTPSessionManager task group was not initialized`.
- **Do not remove the Monarch domain patch** at `server.py:64` (CLAUDE.md gotcha 1).
- **Route order is load-bearing.** Explicit `Route(...)` entries must stay before `Mount("/", app=mcp_asgi)` or REST 404s.
- **No pytest.** It is not installed. Tests are self-contained assert-based scripts run with `python test_x.py`, matching `test_reauth.py`.
- **Tests must not touch the network or the live Monarch account.** Set `MONARCH_TOKEN` and remove `MONARCH_EMAIL` / `MONARCH_PASSWORD` / `MONARCH_MFA_SECRET` before importing `server` (CLAUDE.md gotcha 7).
- **`server.py` requires `MCP_API_KEY` at import time** (`server.py:50`, `os.environ["MCP_API_KEY"]`). Any test importing it must `os.environ.setdefault("MCP_API_KEY", ...)` first.
- **Style:** `from __future__ import annotations` is present; type hints on module constants and function signatures; deliberate simplifications marked with a `# ponytail:` comment naming the ceiling.
- Exact import paths (none of these except `MultiAuth` are re-exported at the `fastmcp.server.auth` top level):
  ```python
  from fastmcp.server.auth import AccessToken, MultiAuth, OAuthProxy, StaticTokenVerifier
  from fastmcp.server.auth.providers.github import GitHubTokenVerifier
  ```

---

### Task 1: GitHub login allowlist verifier

The highest-severity component in the design. Without it, any GitHub account on earth that finds the URL completes the OAuth flow and reads the Monarch account. Built test-first.

**Files:**
- Modify: `server.py` — new config constant near line 57, new section before `# --- FastMCP instance ---` (line ~349)
- Test: `test_github_allowlist.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `server.AllowlistedGitHubTokenVerifier(allowed_login: str, **kwargs)` with `async verify_token(token: str) -> AccessToken | None`; module constant `server.GITHUB_ALLOWED_USER: str | None`.

- [ ] **Step 1: Write the failing test**

Create `test_github_allowlist.py`:

```python
"""
Self-check for the GitHub login allowlist in server.py.

Run:  python test_github_allowlist.py

No network, no GitHub account, no Monarch account needed. The parent verifier's
verify_token is replaced with a fake that returns a canned AccessToken, so the
real https://api.github.com/user call never happens.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("MCP_API_KEY", "test-key")
# Token path only: no email/password means _reauth can never reach live Monarch.
os.environ.setdefault("MONARCH_TOKEN", "test-token")
for _var in ("MONARCH_EMAIL", "MONARCH_PASSWORD", "MONARCH_MFA_SECRET"):
    os.environ.pop(_var, None)
# server.py builds its auth provider at import time and raises if OAuth is only
# half-configured. On a machine that has real OAuth env vars -- the deploy host --
# that would abort the import before any test runs.
for _var in (
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_ALLOWED_USER",
    "PUBLIC_BASE_URL",
):
    os.environ.pop(_var, None)

import server  # noqa: E402
from fastmcp.server.auth import AccessToken  # noqa: E402
from fastmcp.server.auth.providers.github import GitHubTokenVerifier  # noqa: E402


def _stub_parent(claims: dict[str, object] | None) -> None:
    """Replace GitHubTokenVerifier.verify_token with a canned response."""

    async def _verify(self, token: str) -> AccessToken | None:  # noqa: ANN001
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id="12345",
            scopes=["user"],
            claims=claims,
        )

    GitHubTokenVerifier.verify_token = _verify  # type: ignore[method-assign]


def test_allowed_login_passes() -> None:
    _stub_parent({"login": "richardadonnell", "sub": "12345"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    result = asyncio.run(v.verify_token("tok"))
    assert result is not None, "the allowed login must be admitted"
    assert result.claims["login"] == "richardadonnell"


def test_other_login_rejected() -> None:
    _stub_parent({"login": "someone-else", "sub": "99999"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None, (
        "a non-allowlisted GitHub login must be refused"
    )


def test_login_match_is_case_insensitive() -> None:
    # GitHub usernames are case-insensitive; RichardADonnell is the same account.
    _stub_parent({"login": "RichardADonnell", "sub": "12345"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is not None


def test_parent_rejection_propagates() -> None:
    _stub_parent(None)
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None


def test_missing_login_claim_rejected() -> None:
    _stub_parent({"sub": "12345"})  # no "login" key at all
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None, (
        "absent login claim must fail closed, not match"
    )


def test_empty_allowlist_refuses_to_construct() -> None:
    try:
        server.AllowlistedGitHubTokenVerifier(allowed_login="")
    except ValueError:
        return
    raise AssertionError("an empty allowed_login must raise, never match everyone")


if __name__ == "__main__":
    test_allowed_login_passes()
    test_other_login_rejected()
    test_login_match_is_case_insensitive()
    test_parent_rejection_propagates()
    test_missing_login_claim_rejected()
    test_empty_allowlist_refuses_to_construct()
    print("OK  github allowlist: 6/6")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_github_allowlist.py`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'AllowlistedGitHubTokenVerifier'`

- [ ] **Step 3: Add the config constant**

In `server.py`, in the `# --- Config ---` block, immediately after the `MONARCH_MFA_SECRET` line (~line 57) and before `PORT`:

```python
# OAuth (optional). When GITHUB_CLIENT_ID is unset, /mcp keeps its pre-OAuth
# behavior and APIKeyMiddleware stays the only guard -- see _build_auth().
GITHUB_CLIENT_ID: str | None = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET: str | None = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_ALLOWED_USER: str | None = os.getenv("GITHUB_ALLOWED_USER")
PUBLIC_BASE_URL: str | None = os.getenv("PUBLIC_BASE_URL")
```

- [ ] **Step 4: Add the imports this task actually uses**

In the import block, after `from fastmcp import FastMCP` (line 35):

```python
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
```

Only these two. `MultiAuth`, `OAuthProxy`, and `StaticTokenVerifier` arrive in Task 2,
where they are first used — adding them here would commit three unused imports.

- [ ] **Step 5: Write the verifier**

In `server.py`, add a new section immediately before `# --- FastMCP instance ---`:

```python
# --- OAuth ---------------------------------------------------------------------


class AllowlistedGitHubTokenVerifier(GitHubTokenVerifier):
    """GitHub token verifier that admits exactly one GitHub login.

    OAuthProxy is reachable by anyone who finds the URL, and GitHub will happily
    authenticate any of its users. This narrows that to one account.

    Returning None rather than raising is deliberate: FastMCP turns None into a
    401 with a WWW-Authenticate header, which is the handshake Claude needs to
    offer a Connect prompt. An exception here would surface as a 500 instead.
    """

    def __init__(self, allowed_login: str, **kwargs: Any) -> None:
        if not allowed_login:
            raise ValueError(
                "allowed_login must be a non-empty GitHub username; an empty "
                "value would admit every GitHub account"
            )
        super().__init__(**kwargs)
        # GitHub usernames are case-insensitive.
        self._allowed_login = allowed_login.casefold()

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        login = result.claims.get("login")
        if not isinstance(login, str) or login.casefold() != self._allowed_login:
            logger.warning(
                "Refused GitHub login %r (allowlist: %r)", login, self._allowed_login
            )
            return None
        return result
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python test_github_allowlist.py`
Expected: `OK  github allowlist: 6/6`

- [ ] **Step 7: Confirm nothing else broke**

Run: `python test_reauth.py && python test_update_transaction_kwargs.py`
Expected: both print their existing success output.

- [ ] **Step 8: Commit**

```bash
git add server.py test_github_allowlist.py
git commit -m "feat: add single-login GitHub allowlist verifier"
```

---

### Task 2: Build the OAuth provider and wire it into FastMCP

**Files:**
- Modify: `server.py` — extend the `# --- OAuth ---` section from Task 1; change the `mcp = FastMCP(...)` call at line ~351
- Test: `test_github_allowlist.py` (extend)

**Interfaces:**
- Consumes: `AllowlistedGitHubTokenVerifier`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_ALLOWED_USER`, `PUBLIC_BASE_URL` from Task 1.
- Produces: `server._build_auth() -> MultiAuth | None` and module-level `server._AUTH: MultiAuth | None`. Task 3's middleware reads `_AUTH`.

- [ ] **Step 1: Write the failing test**

Append to `test_github_allowlist.py`, before the `if __name__ == "__main__":` block:

```python
def _set_oauth_env(**overrides: str | None) -> None:
    """Point server's module-level OAuth config at test values."""
    defaults = {
        "GITHUB_CLIENT_ID": "Ov23liTEST",
        "GITHUB_CLIENT_SECRET": "secret",
        "GITHUB_ALLOWED_USER": "richardadonnell",
        "PUBLIC_BASE_URL": "https://mm-mcp.example.com",
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        setattr(server, name, value)


def test_build_auth_returns_none_without_client_id() -> None:
    _set_oauth_env(GITHUB_CLIENT_ID=None)
    assert server._build_auth() is None, (
        "no GITHUB_CLIENT_ID must mean no auth provider, preserving pre-OAuth behavior"
    )


def test_build_auth_rejects_partial_config() -> None:
    _set_oauth_env(GITHUB_ALLOWED_USER=None)
    try:
        server._build_auth()
    except RuntimeError as exc:
        assert "GITHUB_ALLOWED_USER" in str(exc), (
            "the error must name the missing variable"
        )
        return
    raise AssertionError(
        "OAuth half-configured must fail loudly, not silently admit everyone"
    )


def test_build_auth_composes_multiauth() -> None:
    _set_oauth_env()
    auth = server._build_auth()
    assert auth is not None
    assert isinstance(auth, MultiAuth)
```

Add `MultiAuth` to the test's imports:

```python
from fastmcp.server.auth import AccessToken, MultiAuth  # noqa: E402
```

And add the three calls to the `__main__` block, updating the count line:

```python
    test_build_auth_returns_none_without_client_id()
    test_build_auth_rejects_partial_config()
    test_build_auth_composes_multiauth()
    print("OK  github allowlist: 9/9")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_github_allowlist.py`
Expected: FAIL with `AttributeError: module 'server' has no attribute '_build_auth'`

- [ ] **Step 3: Write `_build_auth()`**

Append to the `# --- OAuth ---` section in `server.py`, after the verifier class:

```python
def _build_auth() -> MultiAuth | None:
    """Compose OAuth for Claude's connector UI with the legacy key for n8n.

    Returns None when GITHUB_CLIENT_ID is unset, which leaves /mcp exactly as it
    behaved before OAuth existed. That is the documented rollback.
    """
    if not GITHUB_CLIENT_ID:
        return None

    missing = [
        name
        for name, value in (
            ("GITHUB_CLIENT_SECRET", GITHUB_CLIENT_SECRET),
            ("GITHUB_ALLOWED_USER", GITHUB_ALLOWED_USER),
            ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "GITHUB_CLIENT_ID is set, so these are required too: " + ", ".join(missing)
        )

    verifier = AllowlistedGitHubTokenVerifier(
        allowed_login=GITHUB_ALLOWED_USER,  # type: ignore[arg-type]
        # ponytail: 5-minute cache. Uncached, GitHubTokenVerifier spends two
        # api.github.com calls per MCP request against a 5000/hr budget.
        cache_ttl_seconds=300,
    )

    proxy = OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=GITHUB_CLIENT_ID,
        upstream_client_secret=GITHUB_CLIENT_SECRET,
        token_verifier=verifier,
        base_url=PUBLIC_BASE_URL,
        redirect_path="/auth/callback",
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            # Claude Code binds an ephemeral loopback port (RFC 8252).
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
    )

    return MultiAuth(
        server=proxy,
        verifiers=[
            # client_id is read with a bare subscript by StaticTokenVerifier;
            # omitting it raises KeyError instead of failing auth cleanly.
            StaticTokenVerifier(
                {MCP_API_KEY: {"client_id": "legacy-api-key", "scopes": []}}
            )
        ],
    )


_AUTH: MultiAuth | None = _build_auth()
```

- [ ] **Step 4: Pass it to FastMCP**

Change the `mcp = FastMCP(...)` call (line ~351) to add `auth=_AUTH` as the final argument, after `instructions=(...)`:

```python
mcp = FastMCP(
    "monarch_money_mcp",
    instructions=(
        "Tools for querying Monarch Money personal finance data: accounts, balances, "
        "transactions, cashflow, budgets, net worth, recurring subscriptions, and "
        "investment holdings. Use ISO 8601 dates (YYYY-MM-DD) for all date parameters. "
        "Default date range when unspecified: current calendar month."
    ),
    auth=_AUTH,
)
```

Note: `FastMCP.__init__` does no runtime validation of `auth=` (`fastmcp/server/server.py:424`), so a wrong type here fails later and further away. Get it right at construction.

- [ ] **Step 5: Run test to verify it passes**

Run: `python test_github_allowlist.py`
Expected: `OK  github allowlist: 9/9`

- [ ] **Step 6: Verify the server still boots with OAuth off**

Run: `MCP_API_KEY=x MONARCH_TOKEN=y python -c "import server; print('auth =', server._AUTH)"`
Expected: `auth = None`

- [ ] **Step 7: Commit**

```bash
git add server.py test_github_allowlist.py
git commit -m "feat: compose GitHub OAuth proxy with legacy key via MultiAuth"
```

---

### Task 3: Scope APIKeyMiddleware conditionally

The spec originally called this a two-line change. It is not. Narrowing the middleware to `/api/*` unconditionally opens `/mcp` completely whenever OAuth is disabled, because `auth=None` means FastMCP installs no auth middleware of its own. The guard must depend on whether OAuth is actually active.

**Files:**
- Modify: `server.py` — `APIKeyMiddleware.dispatch` at lines 721-729
- Test: `test_github_allowlist.py` (extend)

**Interfaces:**
- Consumes: `server._AUTH` from Task 2.
- Produces: no new names. Behavior change only.

- [ ] **Step 1: Write the failing test**

Append to `test_github_allowlist.py`, before `if __name__ == "__main__":`:

```python
class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    def __init__(self, path: str, auth: str = "") -> None:
        self.url = _FakeURL(path)
        self.headers = {"Authorization": auth} if auth else {}


def _dispatch(path: str, auth_header: str, oauth_on: bool) -> int:
    """Run APIKeyMiddleware.dispatch and return the resulting status code."""
    original = server._AUTH
    server._AUTH = object() if oauth_on else None
    try:
        mw = server.APIKeyMiddleware(app=None)

        async def _next(_request: object) -> object:
            return type("R", (), {"status_code": 200})()

        result = asyncio.run(_dispatch_async(mw, path, auth_header, _next))
        return result.status_code
    finally:
        server._AUTH = original


async def _dispatch_async(mw, path, auth_header, call_next):  # noqa: ANN001
    return await mw.dispatch(_FakeRequest(path, auth_header), call_next)


def test_health_is_public_in_both_regimes() -> None:
    for oauth_on in (True, False):
        assert _dispatch("/health", "", oauth_on) == 200


def test_api_requires_key_in_both_regimes() -> None:
    # Read the key off the module rather than hardcoding "test-key": if the
    # caller's shell already exports MCP_API_KEY, setdefault above is a no-op
    # and a hardcoded literal would fail spuriously.
    good = f"Bearer {server.MCP_API_KEY}"
    for oauth_on in (True, False):
        assert _dispatch("/api/accounts", "", oauth_on) == 401
        assert _dispatch("/api/accounts", good, oauth_on) == 200, (
            "the real key must still open /api/*"
        )


def test_mcp_is_still_guarded_when_oauth_is_off() -> None:
    # The rollback path. FastMCP installs no auth middleware when auth=None,
    # so this middleware must remain the guard on /mcp.
    assert _dispatch("/mcp", "", oauth_on=False) == 401, (
        "with OAuth disabled, /mcp must not be open to the world"
    )


def test_mcp_is_delegated_when_oauth_is_on() -> None:
    # FastMCP's own auth middleware owns /mcp, and accepts the legacy key
    # through MultiAuth, so this middleware must step aside.
    assert _dispatch("/mcp", "", oauth_on=True) == 200


def test_oauth_endpoints_are_public_when_oauth_is_on() -> None:
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/authorize",
        "/token",
        "/register",
        "/auth/callback",
    ):
        assert _dispatch(path, "", oauth_on=True) == 200, (
            f"{path} must be reachable unauthenticated for OAuth discovery"
        )
```

Add the five calls to `__main__` and bump the count to `14/14`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_github_allowlist.py`
Expected: FAIL on `test_mcp_is_delegated_when_oauth_is_on` — the current middleware returns 401 for `/mcp` regardless.

- [ ] **Step 3: Rewrite the middleware**

Replace `APIKeyMiddleware.dispatch` (`server.py:722-729`) with:

```python
class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Health check is always public
        if path == "/health":
            return await call_next(request)
        # With OAuth active, FastMCP's own auth middleware owns /mcp and the
        # OAuth endpoints -- and accepts this same key via MultiAuth. Without
        # it, auth=None means FastMCP guards nothing, so this middleware stays
        # the only thing standing in front of /mcp.
        if _AUTH is not None and not path.startswith("/api/"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:] == MCP_API_KEY):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_github_allowlist.py`
Expected: `OK  github allowlist: 14/14`

- [ ] **Step 5: Confirm the existing suite is unaffected**

Run: `python test_reauth.py && python test_update_transaction_kwargs.py`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add server.py test_github_allowlist.py
git commit -m "fix: scope APIKeyMiddleware to /api/* only while OAuth is active"
```

---

### Task 4: Deployment configuration

**Files:**
- Modify: `docker-compose.yml` (27 lines, no `volumes:` key exists yet)
- Modify: `.env.example`

No `Dockerfile` change is needed: it runs as root with no `USER` instruction, and Docker auto-creates a missing named-volume mount point.

**Interfaces:**
- Consumes: the five environment variable names from Task 1.
- Produces: a named volume `oauth-state` mounted at `/data`, and `FASTMCP_HOME=/data`.

- [ ] **Step 1: Add the volume mount to the service**

In `docker-compose.yml`, insert between `env_file:` (ends line 8) and `environment:` (line 9):

```yaml
    volumes:
      # OAuth state: Claude's client registration and the refresh token. Without
      # this, every rebuild wipes them and the connector must be re-authorized.
      - oauth-state:/data
```

- [ ] **Step 2: Add the environment variables**

In the same file, append inside the `environment:` list after the `PORT` line:

```yaml
      # OAuth for Claude custom connectors. Leave GITHUB_CLIENT_ID empty to
      # disable OAuth entirely; /mcp then falls back to MCP_API_KEY only.
      - GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID:-}
      - GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET:-}
      - GITHUB_ALLOWED_USER=${GITHUB_ALLOWED_USER:-}
      - PUBLIC_BASE_URL=${PUBLIC_BASE_URL:-}
      # Where OAuthProxy persists its encrypted state. Must match the volume above.
      - FASTMCP_HOME=${FASTMCP_HOME:-/data}
```

- [ ] **Step 3: Declare the named volume**

Append at file scope (column 0), after the healthcheck block at the end of the file:

```yaml

volumes:
  oauth-state:
```

- [ ] **Step 4: Validate the compose file parses**

Run: `docker compose config >/dev/null && echo "compose OK"`
Expected: `compose OK`

- [ ] **Step 5: Document the variables in `.env.example`**

Append to `.env.example`, matching its existing `# ── Section ──` divider style:

```
# ── Claude custom connector OAuth (optional) ──────────────────────────────────
# Only needed to add this server as a connector in Claude Desktop / web / mobile.
# Leave GITHUB_CLIENT_ID blank to disable OAuth; /mcp then uses MCP_API_KEY only.
#
# Create a GitHub OAuth App at https://github.com/settings/developers with
# Authorization callback URL exactly:  {PUBLIC_BASE_URL}/auth/callback
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=

# The one GitHub username allowed to connect. Anyone else who completes the
# GitHub login is refused. Never leave this blank while GITHUB_CLIENT_ID is set.
GITHUB_ALLOWED_USER=

# Public HTTPS base URL. Must match what you type into Claude, exactly.
PUBLIC_BASE_URL=https://mm-mcp.richardadonnell.com

# Where OAuth state is persisted inside the container (mounted volume).
FASTMCP_HOME=/data
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "chore: add OAuth env vars and a persistent volume for OAuth state"
```

---

### Task 5: Extend verify_server.sh with OAuth assertions

`verify_server.sh` has **no skip idiom** — every `head2` block currently resolves to at least one `pass`/`fail`. This task introduces a one-line `skip()` matching the existing helper shape, and a header-value helper, because `has_session_hdr()` only tests presence.

**Files:**
- Modify: `verify_server.sh` — helpers near line 38, new sections 9-11 after section 8 (ends line 425), before the summary block (line 427)

**Interfaces:**
- Consumes: `$BASE`, `$TMP`, `$AUTH`, `pass`, `fail`, `info`, `head2`, `jget` — all already defined.
- Produces: `skip <msg>` and `hdr_value <hdrfile> <header-name>`.

- [ ] **Step 1: Add the two helpers**

In `verify_server.sh`, immediately after the `head2()` definition (line 39):

```bash
skip() { printf '%sSKIP%s  %s\n' "$YEL" "$RST" "$*"; }
hdr_value() {
  grep -i "^$2:" "$1" 2>/dev/null | head -n1 | sed "s/^[^:]*:[[:space:]]*//" | tr -d '\r'
}
```

`skip()` deliberately touches neither `TOTAL` nor `FAILED` — a skipped check is not a passed check, and the summary line must not claim otherwise.

- [ ] **Step 2: Add section 9 — protected resource metadata**

Insert after section 8 ends (line 425), before the summary:

```bash
# --- 9. OAuth protected resource metadata (unauthenticated) ------------------
head2 "9. OAuth: /.well-known/oauth-protected-resource/mcp (no auth)"
prm_code=$(curl -sS -o "$TMP/prm.raw" -w '%{http_code}' \
  "$BASE/.well-known/oauth-protected-resource/mcp" 2>/dev/null) || prm_code=000
de_sse "$TMP/prm.raw" "$TMP/prm.json"
if [ "$prm_code" = "404" ]; then
  skip "OAuth not enabled on this server (PRM 404) -- skipping checks 9-11"
  OAUTH_ON=false
else
  OAUTH_ON=true
  resource="$(jget "$TMP/prm.json" '.resource')"
  if [ "$prm_code" != "200" ]; then
    fail "protected resource metadata -> $prm_code (expected 200)"
    info "$(snippet "$TMP/prm.raw")"
  elif [ "$resource" = "$BASE/mcp" ]; then
    pass "protected resource metadata -> 200, resource: $resource"
  else
    fail "PRM resource is '${resource:-<absent>}' (expected $BASE/mcp)"
    info "a mismatch here is why Claude says 'Couldn't reach the MCP server'"
  fi
  authsrv="$(jget "$TMP/prm.json" '.authorization_servers')"
  if [ -n "$authsrv" ]; then
    pass "PRM advertises authorization_servers: $authsrv"
  else
    fail "PRM has no authorization_servers -- Claude cannot discover the AS"
  fi
fi
```

- [ ] **Step 3: Add section 10 — the 401 challenge**

```bash
# --- 10. Unauthenticated /mcp returns a WWW-Authenticate challenge -----------
head2 "10. OAuth: unauthenticated POST /mcp -> 401 + WWW-Authenticate"
if ! $OAUTH_ON; then
  skip "OAuth not enabled -- skipping"
else
  ch_code=$(curl -sS -o "$TMP/challenge.raw" -D "$TMP/challenge.hdr" \
    -w '%{http_code}' -X POST "$BASE/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2026-07-28' \
    --data-binary '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    2>/dev/null) || ch_code=000
  wwwauth="$(hdr_value "$TMP/challenge.hdr" 'www-authenticate')"
  if [ "$ch_code" != "401" ]; then
    fail "unauthenticated /mcp -> $ch_code (expected 401)"
    info "$(snippet "$TMP/challenge.raw")"
  elif printf '%s' "$wwwauth" | grep -q 'resource_metadata='; then
    pass "401 with WWW-Authenticate: $wwwauth"
  else
    fail "401 but WWW-Authenticate lacks resource_metadata: ${wwwauth:-<absent>}"
    info "without it Claude shows an error instead of a Connect prompt"
  fi
fi
```

- [ ] **Step 4: Add section 11 — authorization server metadata**

```bash
# --- 11. Authorization server metadata ---------------------------------------
head2 "11. OAuth: /.well-known/oauth-authorization-server"
if ! $OAUTH_ON; then
  skip "OAuth not enabled -- skipping"
else
  as_code=$(curl -sS -o "$TMP/asmeta.raw" -w '%{http_code}' \
    "$BASE/.well-known/oauth-authorization-server" 2>/dev/null) || as_code=000
  de_sse "$TMP/asmeta.raw" "$TMP/asmeta.json"
  pkce="$(jget "$TMP/asmeta.json" '.code_challenge_methods_supported')"
  reg="$(jget "$TMP/asmeta.json" '.registration_endpoint')"
  if [ "$as_code" != "200" ]; then
    fail "authorization server metadata -> $as_code (expected 200)"
    info "$(snippet "$TMP/asmeta.raw")"
  else
    if printf '%s' "$pkce" | grep -q 'S256'; then
      pass "AS metadata advertises S256 PKCE"
    else
      fail "AS metadata lacks S256 in code_challenge_methods_supported: ${pkce:-<absent>}"
    fi
    if [ -n "$reg" ]; then
      pass "AS metadata advertises registration_endpoint: $reg"
    else
      fail "AS metadata has no registration_endpoint -- Claude requires DCR or CIMD"
    fi
  fi
fi
```

- [ ] **Step 5: Run against the current production server (OAuth not yet deployed)**

Run: `./verify_server.sh https://mm-mcp.richardadonnell.com`
Expected: the original 14 checks pass; sections 9-11 print `SKIP`; summary reads `14/14 checks passed`.

- [ ] **Step 6: Commit**

```bash
git add verify_server.sh
git commit -m "test: add OAuth discovery and 401-challenge assertions to verify_server.sh"
```

---

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` — Commands block (line ~18), Gotchas (append item 9 after line 86), Env vars (lines 118-122)
- Modify: `server.py` — module docstring env var list (lines 11-19)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing consumed by code.

- [ ] **Step 1: Update the assertion count in Commands**

In `CLAUDE.md`, change:

```
./verify_server.sh http://localhost:8000          # 14 assertions against a running server
```

to:

```
./verify_server.sh http://localhost:8000          # 17 assertions (14 + 3 OAuth, skipped when OAuth is off)
```

And add the new test to the list below it:

```
python test_github_allowlist.py                   # no network, no account needed
```

- [ ] **Step 2: Add gotcha 9**

Append to `CLAUDE.md` after gotcha 8's paragraph (line 86):

```markdown
9. **Two auth régimes, and the split is conditional.** `/api/*` is guarded by
   `APIKeyMiddleware`; `/mcp` and the OAuth endpoints are guarded by FastMCP's
   own middleware via `MultiAuth`, which accepts the same `MCP_API_KEY`. But
   that split only applies when OAuth is on. When `GITHUB_CLIENT_ID` is unset,
   `_AUTH is None`, FastMCP installs no auth middleware at all, and
   `APIKeyMiddleware` must keep guarding `/mcp` itself. Narrowing it to `/api/*`
   unconditionally would leave `/mcp` open on the rollback path. See
   `APIKeyMiddleware.dispatch` in `server.py` (cited by symbol, not line — the
   line numbers in gotchas 2 and 3 above have already drifted). New routes:
   anything under `/api/` is covered automatically, anything else is not.
```

- [ ] **Step 3: Update the Env vars section**

Replace the `## Env vars` section body with:

```markdown
Required: `MCP_API_KEY`. Auth: either `MONARCH_TOKEN` (preferred, stateless)
OR `MONARCH_EMAIL` + `MONARCH_PASSWORD` (+ `MONARCH_MFA_SECRET` if 2FA).

OAuth (all optional; only needed to add this server as a Claude connector):
`GITHUB_CLIENT_ID` acts as the on/off switch — when it is set,
`GITHUB_CLIENT_SECRET`, `GITHUB_ALLOWED_USER`, and `PUBLIC_BASE_URL` become
required and the server refuses to boot without them. `FASTMCP_HOME=/data`
points OAuth state at the mounted volume.

Full reference in `.env.example`. README covers user-facing setup.
```

- [ ] **Step 4: Update the `server.py` module docstring**

In the `Env vars:` block of the docstring (lines 11-19), after the `MCP_API_KEY` line:

```
  GITHUB_CLIENT_ID   optional; enables OAuth for Claude custom connectors
  GITHUB_CLIENT_SECRET  required when GITHUB_CLIENT_ID is set
  GITHUB_ALLOWED_USER   required when GITHUB_CLIENT_ID is set; the one GitHub login admitted
  PUBLIC_BASE_URL       required when GITHUB_CLIENT_ID is set; must match the URL entered in Claude
```

And change the `Auth:` line to:

```
Auth: Authorization: Bearer {MCP_API_KEY} on /api/*. /mcp accepts that key or a
GitHub OAuth token when GITHUB_CLIENT_ID is set. /health is always public.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md server.py
git commit -m "docs: record the two auth regimes and the OAuth env vars"
```

---

## Deployment runbook

Not a code task. Run after Tasks 1-6 are merged.

- [ ] Create a GitHub OAuth App at https://github.com/settings/developers
      Authorization callback URL: `https://mm-mcp.richardadonnell.com/auth/callback`
- [ ] Set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_ALLOWED_USER`, `PUBLIC_BASE_URL`, `FASTMCP_HOME` in Coolify
- [ ] Confirm Traefik passes `/.well-known/*` through and does not rate-limit,
      challenge, or geo-block `160.79.104.0/21` — Anthropic's discovery, registration,
      and token calls come from there with a 10-second budget. A WAF in front of the
      app breaks the flow while `/health` still looks fine.
- [ ] Deploy
- [ ] `./verify_server.sh https://mm-mcp.richardadonnell.com` — expect 17/17
- [ ] Claude Desktop → Settings → Connectors → + → Add custom connector →
      `https://mm-mcp.richardadonnell.com/mcp` → leave Advanced settings empty
- [ ] Confirm the connector appears on Claude mobile after next login
- [ ] Confirm the existing Claude Code `--header` entry still connects
- [ ] Redeploy once and confirm no re-authorization is required (proves the volume works)
