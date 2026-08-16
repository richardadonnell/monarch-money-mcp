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

import server  # noqa: E402
from gql.transport.exceptions import TransportServerError  # noqa: E402


def _expired() -> TransportServerError:
    """The error Monarch actually produced when the stored token went stale."""
    return TransportServerError(
        "401, message='Unauthorized', url='https://api.monarch.com/graphql'", 401
    )


class Harness:
    """Swaps server._login for a counter and resets module auth state."""

    def __init__(self, email: str | None = "test@example.com") -> None:
        self.logins = 0
        self._email = email

    async def _fake_login(self) -> None:
        self.logins += 1
        await asyncio.sleep(0)  # yield, so concurrent callers can interleave
        server._monarch_ready = True

    def __enter__(self) -> "Harness":
        self._real_login = server._login
        self._real_email = server.MONARCH_EMAIL
        server._login = self._fake_login
        server.MONARCH_EMAIL = self._email
        server._monarch_ready = True
        server._auth_epoch = 0
        return self

    def __exit__(self, *_exc: object) -> None:
        server._login = self._real_login
        server.MONARCH_EMAIL = self._real_email


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
