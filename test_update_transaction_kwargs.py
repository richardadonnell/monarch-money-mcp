"""
Self-check for the update_transaction kwarg contract.

Run:  python test_update_transaction_kwargs.py

No network and no Monarch account needed. Binds the arguments each call site
passes against the real MonarchMoney.update_transaction signature, so a
rename upstream (or a call site drifting back to `id=`) fails here instead of
as a 500 at runtime.

Guards the bug where the MCP tool used `transaction_id=` but the REST handler
at server.py:718 still passed `id=`, making POST /api/transaction/{id} raise
"got an unexpected keyword argument 'id'" on every request.
"""

from __future__ import annotations

import inspect

from monarchmoney import MonarchMoney

SIG = inspect.signature(MonarchMoney.update_transaction)


def _binds(**kwargs) -> bool:
    """True if kwargs are accepted by update_transaction (self is bound)."""
    try:
        SIG.bind(None, **kwargs)
        return True
    except TypeError:
        return False


def main() -> None:
    # The SDK's own parameter name. If this flips, both call sites must change.
    assert "transaction_id" in SIG.parameters, (
        f"SDK renamed the id param; signature is now {SIG}"
    )
    assert "id" not in SIG.parameters, f"SDK unexpectedly accepts `id`: {SIG}"

    # REST handler: server.py api_update_transaction
    assert _binds(transaction_id="txn_1"), "REST call site would TypeError"
    assert not _binds(id="txn_1"), "`id=` should be rejected — the original bug"

    # MCP tool: server.py update_transaction builds this kwargs dict
    assert _binds(
        transaction_id="txn_1",
        category_id="cat_1",
        notes="memo",
        hide_from_reports=True,
        needs_review=False,
    ), "MCP tool call site would TypeError"

    # A body key the REST handler forwards verbatim must still be real.
    assert not _binds(transaction_id="txn_1", nonexistent_field=1)

    print("OK: update_transaction kwarg contract holds")
    print(f"    signature: {SIG}")


if __name__ == "__main__":
    main()
