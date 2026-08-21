"""
Self-check for the 401 re-auth path in server.py.

Run:  python test_reauth.py

No network and no Monarch account needed. The client methods are replaced with
fakes that raise the exact exception gql raises on an expired session
(TransportServerError with .code == 401), and _login is replaced with a counter.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("MCP_API_KEY", "test-key")
os.environ.setdefault("MONARCH_EMAIL", "test@example.com")
os.environ.setdefault("MONARCH_PASSWORD", "hunter2")
os.environ.pop("MONARCH_TOKEN", None)  # exercise the credential path
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
from gql.transport.exceptions import TransportServerError  # noqa: E402
from monarchmoney import LoginFailedException  # noqa: E402


def _expired() -> TransportServerError:
    """The error Monarch actually produced when the stored token went stale."""
    return TransportServerError(
        "401, message='Unauthorized', url='https://api.monarch.com/graphql'", 401
    )


class Harness:
    """Swaps server._login for a counter and resets module auth state."""

    def __init__(
        self,
        email: str | None = "test@example.com",
        login_error: Exception | None = None,
    ) -> None:
        self.logins = 0
        self._email = email
        self._login_error = login_error

    async def _fake_login(self) -> None:
        self.logins += 1
        await asyncio.sleep(0)  # yield, so concurrent callers can interleave
        if self._login_error is not None:
            raise self._login_error
        server._monarch_ready = True

    def __enter__(self) -> "Harness":
        self._real_login = server._login
        self._real_email = server.MONARCH_EMAIL
        server._login = self._fake_login
        server.MONARCH_EMAIL = self._email
        server._monarch_ready = True
        server._auth_epoch = 0
        server._auth_blocked_until = 0.0
        return self

    def __exit__(self, *_exc: object) -> None:
        server._login = self._real_login
        server.MONARCH_EMAIL = self._real_email
        server._auth_blocked_until = 0.0  # never leak a cooldown into the next test


def test_retries_once_after_401() -> None:
    with Harness() as h:
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _expired()
            return {"accounts": []}

        result = asyncio.run(server._call(flaky))

    assert result == {"accounts": []}, result
    assert calls["n"] == 2, f"expected 1 retry, got {calls['n']} calls"
    assert h.logins == 1, f"expected 1 re-login, got {h.logins}"
    assert server._auth_epoch == 1, server._auth_epoch


def test_non_auth_error_propagates_without_relogin() -> None:
    with Harness() as h:
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            raise TransportServerError("500, message='Server Error'", 500)

        try:
            asyncio.run(server._call(boom))
        except TransportServerError as exc:
            assert exc.code == 500, exc.code
        else:
            raise AssertionError("500 should propagate, not be retried")

    assert calls["n"] == 1, f"500 must not be retried, got {calls['n']} calls"
    assert h.logins == 0, "500 must not trigger a re-login"


def test_persistent_401_gives_up_after_one_retry() -> None:
    with Harness() as h:
        calls = {"n": 0}

        async def always_401():
            calls["n"] += 1
            raise _expired()

        try:
            asyncio.run(server._call(always_401))
        except TransportServerError as exc:
            assert exc.code == 401, exc.code
        else:
            raise AssertionError("a persistent 401 should surface, not loop")

    assert calls["n"] == 2, f"expected exactly one retry, got {calls['n']} calls"
    assert h.logins == 1, h.logins


def test_token_only_deploy_does_not_retry() -> None:
    # No credentials to re-mint with: retrying would replay the same dead token.
    with Harness(email=None) as h:
        calls = {"n": 0}

        async def always_401():
            calls["n"] += 1
            raise _expired()

        try:
            asyncio.run(server._call(always_401))
        except TransportServerError:
            pass
        else:
            raise AssertionError("401 should surface when there is no way to re-auth")

    assert calls["n"] == 1, f"must not retry without credentials, got {calls['n']}"
    assert h.logins == 0, h.logins


def test_concurrent_401s_collapse_into_one_login() -> None:
    # When a session expires, every in-flight request 401s at once. They must
    # share one login: a TOTP code cannot be spent twice.
    with Harness() as h:
        calls = {"n": 0}

        async def session_scoped():
            calls["n"] += 1
            await asyncio.sleep(0)
            if server._auth_epoch == 0:  # still the dead session
                raise _expired()
            return "ok"

        async def main():
            return await asyncio.gather(
                *(server._call(session_scoped) for _ in range(5))
            )

        results = asyncio.run(main())

    assert results == ["ok"] * 5, results
    assert h.logins == 1, f"expected 1 shared login, got {h.logins}"
    assert server._auth_epoch == 1, server._auth_epoch


def test_failed_relogin_starts_cooldown() -> None:
    """
    A rejected login must not be retried on every subsequent request. Without a
    cooldown a stuck credential turns each incoming request into its own login
    attempt against Monarch, which is how you get rate-limited or locked out.
    """
    dead_credentials = LoginFailedException("HTTP Code 401: Unauthorized")

    async def always_401():
        raise _expired()

    with Harness(login_error=dead_credentials) as h:
        # First request: 401 -> re-login attempt -> login is rejected.
        try:
            asyncio.run(server._call(always_401))
        except LoginFailedException:
            pass
        else:
            raise AssertionError("a failed re-login should surface")

        assert h.logins == 1, h.logins
        assert server._auth_blocked_until > 0, "a failed login must start a cooldown"

        # Second request during the cooldown: no new login, original 401 surfaces.
        try:
            asyncio.run(server._call(always_401))
        except TransportServerError as exc:
            assert exc.code == 401, exc.code
        else:
            raise AssertionError("the underlying 401 should surface during cooldown")

        assert h.logins == 1, f"cooldown must suppress the retry, got {h.logins} logins"

        # Once the cooldown lapses, the next 401 may try again.
        server._auth_blocked_until = 0.0
        try:
            asyncio.run(server._call(always_401))
        except LoginFailedException:
            pass

        assert h.logins == 2, f"expected a fresh attempt after cooldown, got {h.logins}"


def test_login_drops_stale_token_header() -> None:
    """
    Regression: the MONARCH_TOKEN branch puts "Authorization: Token <dead>" on the
    client, and monarchmoney POSTs the login with ClientSession(headers=self._headers).
    Monarch then answered 401 without ever reading the credentials, so the retry
    could never recover. Patches mm.login (not _login) to see the real headers.
    """
    real_headers = dict(server.mm._headers)
    real_token = server.MONARCH_TOKEN
    seen: dict[str, dict[str, str]] = {}

    async def fake_login(**_kwargs: object) -> None:
        seen["headers"] = dict(server.mm._headers)  # what the login POST would carry
        server.mm._headers["Authorization"] = "Token fresh-token"  # as _login_user does

    try:
        server.mm.login = fake_login
        server.MONARCH_TOKEN = "dead-token"  # reproduce the prod config
        server._monarch_ready = False  # force _init_monarch through the token branch
        server._auth_epoch = 0

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _expired()
            return "ok"

        result = asyncio.run(server._call(flaky))
        seen["after"] = dict(server.mm._headers)  # capture before the restore below
    finally:
        del server.mm.login
        server.mm._headers.clear()
        server.mm._headers.update(real_headers)
        server.MONARCH_TOKEN = real_token

    assert result == "ok", result
    assert "headers" in seen, "login was never attempted"
    assert "Authorization" not in seen["headers"], (
        f"login POST carried a stale token: {seen['headers'].get('Authorization')!r}"
    )
    assert seen["after"]["Authorization"] == "Token fresh-token", seen["after"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
