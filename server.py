"""
Monarch Money MCP Server
========================
Dual-protocol server:
  - /mcp   FastMCP streamable-HTTP (for Claude Desktop / Allen)
  - /api/* Plain REST endpoints (for n8n HTTP Request nodes)
  - /health Unauthenticated health check

Auth: Authorization: Bearer {MCP_API_KEY} on every route except /health.

Env vars:
  MONARCH_TOKEN      preferred; inject the Monarch bearer token directly (stateless)
  MONARCH_EMAIL      fallback: email for initial login
  MONARCH_PASSWORD   fallback: password for initial login
  MONARCH_MFA_SECRET TOTP secret key for 2FA accounts (Base32 seed, NOT the 6-digit code)
                     Found in: Monarch Settings -> Security -> MFA -> "Two-factor text code"
                     Or in 1Password: Edit entry -> OTP field -> Copy Secret Key
  MCP_API_KEY        required; protects all endpoints
  PORT               optional, defaults to 8000
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastmcp import FastMCP
from monarchmoney import MonarchMoney
from monarchmoney.monarchmoney import MonarchMoneyEndpoints
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("monarch_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]           # required
MONARCH_TOKEN: str | None = os.getenv("MONARCH_TOKEN")
MONARCH_EMAIL: str | None = os.getenv("MONARCH_EMAIL")
MONARCH_PASSWORD: str | None = os.getenv("MONARCH_PASSWORD")
# Raw TOTP secret key (Base32 seed, NOT the 6-digit code).
# Get it from: Monarch Settings -> Security -> MFA -> "Two-factor text code"
# Or from 1Password: Edit the Monarch entry -> OTP field -> Copy Secret Key
MONARCH_MFA_SECRET: str | None = os.getenv("MONARCH_MFA_SECRET")
PORT: int = int(os.getenv("PORT", "8000"))

# --- Monarch client (module-level singleton) ----------------------------------

# Fix: Monarch changed API domain from api.monarchmoney.com to api.monarch.com
# https://github.com/hammem/monarchmoney/issues/184
MonarchMoneyEndpoints.BASE_URL = "https://api.monarch.com"

mm = MonarchMoney()
_monarch_ready: bool = False  # lazy-init flag


async def _init_monarch() -> None:
    """Authenticate the Monarch Money client from env vars. Idempotent."""
    global _monarch_ready
    if _monarch_ready:
        return

    if MONARCH_TOKEN:
        mm.set_token(MONARCH_TOKEN)
        mm._headers["Authorization"] = f"Token {MONARCH_TOKEN}"
        _monarch_ready = True
        logger.info("Monarch: using token from MONARCH_TOKEN env var (stateless)")
        return

    if not (MONARCH_EMAIL and MONARCH_PASSWORD):
        raise RuntimeError(
            "Set MONARCH_TOKEN, or set both MONARCH_EMAIL and MONARCH_PASSWORD"
        )

    try:
        await mm.login(
            email=MONARCH_EMAIL,
            password=MONARCH_PASSWORD,
            use_saved_session=False,
            save_session=False,
            mfa_secret_key=MONARCH_MFA_SECRET,  # None = no 2FA; Base32 secret = auto-TOTP
        )
        _monarch_ready = True
        logger.info("Monarch: logged in with MONARCH_EMAIL / MONARCH_PASSWORD")
        if MONARCH_MFA_SECRET:
            logger.info("Monarch: 2FA TOTP generated automatically from MONARCH_MFA_SECRET")
    except Exception as exc:
        # RequireMFAException is raised when 2FA is enabled but mfa_secret_key was not given
        if "RequireMFA" in type(exc).__name__ or "mfa" in str(exc).lower():
            raise RuntimeError(
                "Monarch requires 2FA but MONARCH_MFA_SECRET is not set. "
                "Set it to the Base32 TOTP secret key (NOT the 6-digit code). "
                "Find it in: Monarch Settings -> Security -> MFA -> 'Two-factor text code', "
                "or in 1Password: Edit entry -> OTP field -> Copy Secret Key."
            ) from exc
        raise


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "monarch_money_mcp",
    instructions=(
        "Tools for querying Monarch Money personal finance data: accounts, balances, "
        "transactions, cashflow, budgets, net worth, recurring subscriptions, and "
        "investment holdings. Use ISO 8601 dates (YYYY-MM-DD) for all date parameters. "
        "Default date range when unspecified: current calendar month."
    ),
)

# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(
    name="get_accounts",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_accounts() -> str:
    """Return all Monarch Money accounts with current balances, types, and metadata."""
    await _init_monarch()
    data = await mm.get_accounts()
    return _json(data)


@mcp.tool(
    name="get_transactions",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_transactions(
    limit: int = 100,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    category_ids: Optional[list[str]] = None,
    account_ids: Optional[list[str]] = None,
    tag_ids: Optional[list[str]] = None,
    has_attachments: Optional[bool] = None,
    has_notes: Optional[bool] = None,
    is_split: Optional[bool] = None,
    is_recurring: Optional[bool] = None,
) -> str:
    """
    Return transactions with optional filtering.

    Args:
        limit: Maximum number of transactions (default 100, max 500).
        start_date: Filter from this date inclusive (YYYY-MM-DD).
        end_date: Filter to this date inclusive (YYYY-MM-DD).
        search: Free-text search across merchant name / description.
        category_ids: Restrict to these category IDs.
        account_ids: Restrict to these account IDs.
        tag_ids: Restrict to these tag IDs.
        has_attachments: Filter by attachment presence.
        has_notes: Filter by note presence.
        is_split: Filter split transactions.
        is_recurring: Filter recurring transactions.
    """
    kwargs: dict[str, Any] = {"limit": limit}
    for k, v in {
        "start_date": start_date,
        "end_date": end_date,
        "search": search,
        "category_ids": category_ids,
        "account_ids": account_ids,
        "tag_ids": tag_ids,
        "has_attachments": has_attachments,
        "has_notes": has_notes,
        "is_split": is_split,
        "is_recurring": is_recurring,
    }.items():
        if v is not None:
            kwargs[k] = v
    await _init_monarch()
    data = await mm.get_transactions(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_cashflow_summary",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_cashflow_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Return cashflow totals (income, expenses, savings rate) for a period.

    Args:
        start_date: Period start (YYYY-MM-DD), defaults to first of current month.
        end_date: Period end (YYYY-MM-DD), defaults to today.
    """
    kwargs: dict[str, Any] = {}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    await _init_monarch()
    data = await mm.get_cashflow_summary(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_cashflow",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_cashflow(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Return detailed cashflow breakdown by category.

    Args:
        start_date: Period start (YYYY-MM-DD).
        end_date: Period end (YYYY-MM-DD).
    """
    kwargs: dict[str, Any] = {}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    await _init_monarch()
    data = await mm.get_cashflow(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_budgets",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_budgets(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Return budget categories with planned amounts and actual spending.

    Args:
        start_date: Budget period start (YYYY-MM-DD).
        end_date: Budget period end (YYYY-MM-DD).
    """
    kwargs: dict[str, Any] = {}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    await _init_monarch()
    data = await mm.get_budgets(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_recurring_transactions",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_recurring_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Return recurring transactions and subscriptions.

    Args:
        start_date: Period start (YYYY-MM-DD).
        end_date: Period end (YYYY-MM-DD).
    """
    kwargs: dict[str, Any] = {}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    await _init_monarch()
    data = await mm.get_recurring_transactions(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_account_holdings",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_account_holdings(account_id: str) -> str:
    """
    Return current holdings for an investment account.

    Args:
        account_id: The Monarch account ID (get from get_accounts first).
    """
    await _init_monarch()
    data = await mm.get_account_holdings(account_id)
    return _json(data)


@mcp.tool(
    name="get_net_worth_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_net_worth_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    Return historical net worth snapshots.

    Args:
        start_date: History start date (YYYY-MM-DD).
        end_date: History end date (YYYY-MM-DD).
    """
    kwargs: dict[str, Any] = {}
    if start_date:
        kwargs["start_date"] = start_date
    if end_date:
        kwargs["end_date"] = end_date
    await _init_monarch()
    data = await mm.get_aggregate_snapshots(**kwargs)
    return _json(data)


@mcp.tool(
    name="get_transaction_categories",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_transaction_categories() -> str:
    """Return all transaction categories with IDs. Use IDs with get_transactions filters or set_budget_amount."""
    await _init_monarch()
    data = await mm.get_transaction_categories()
    return _json(data)


@mcp.tool(
    name="update_transaction",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def update_transaction(
    transaction_id: str,
    category_id: Optional[str] = None,
    notes: Optional[str] = None,
    hide_from_reports: Optional[bool] = None,
    needs_review: Optional[bool] = None,
) -> str:
    """
    Update a transaction's category, notes, or flags.

    Args:
        transaction_id: The transaction ID to update.
        category_id: New category ID (get IDs from get_transaction_categories).
        notes: Free-text notes / memo.
        hide_from_reports: Exclude this transaction from spending reports.
        needs_review: Flag the transaction as needing review.
    """
    kwargs: dict[str, Any] = {"id": transaction_id}
    for k, v in {
        "category_id": category_id,
        "notes": notes,
        "hide_from_reports": hide_from_reports,
        "needs_review": needs_review,
    }.items():
        if v is not None:
            kwargs[k] = v
    await _init_monarch()
    data = await mm.update_transaction(**kwargs)
    return _json(data)


@mcp.tool(
    name="set_budget_amount",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_budget_amount(
    amount: float,
    category_id: str,
    start_date: str,
) -> str:
    """
    Set the monthly budget for a category.

    Args:
        amount: Budget amount in USD.
        category_id: Category ID (get from get_transaction_categories).
        start_date: First day of the target month (YYYY-MM-01).
    """
    await _init_monarch()
    data = await mm.set_budget_amount(
        amount=amount, category_id=category_id, start_date=start_date
    )
    return _json(data)


# --- Auth Middleware ----------------------------------------------------------


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Health check is always public
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:] == MCP_API_KEY):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# --- REST Route Handlers -----------------------------------------------------


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def api_accounts(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        return JSONResponse(await mm.get_accounts())
    except Exception as exc:
        logger.error("api_accounts: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_transactions(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        params = dict(request.query_params)
        limit = int(params.pop("limit", 100))
        return JSONResponse(await mm.get_transactions(limit=limit, **params))
    except Exception as exc:
        logger.error("api_transactions: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_cashflow(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        params = dict(request.query_params)
        return JSONResponse(await mm.get_cashflow(**params))
    except Exception as exc:
        logger.error("api_cashflow: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_budgets(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        params = dict(request.query_params)
        return JSONResponse(await mm.get_budgets(**params))
    except Exception as exc:
        logger.error("api_budgets: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_recurring(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        params = dict(request.query_params)
        return JSONResponse(await mm.get_recurring_transactions(**params))
    except Exception as exc:
        logger.error("api_recurring: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_networth(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        params = dict(request.query_params)
        return JSONResponse(await mm.get_aggregate_snapshots(**params))
    except Exception as exc:
        logger.error("api_networth: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_transaction(request: Request) -> JSONResponse:
    try:
        await _init_monarch()
        txn_id = request.path_params["id"]
        body = await request.json()
        data = await mm.update_transaction(id=txn_id, **body)
        return JSONResponse(data)
    except Exception as exc:
        logger.error("api_update_transaction: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_token(request: Request) -> JSONResponse:
    """Return the current Monarch session token (useful for bootstrapping MONARCH_TOKEN)."""
    await _init_monarch()
    token = getattr(mm, "token", None)
    if token:
        return JSONResponse({"token": token})
    return JSONResponse({"error": "No token available - login first"}, status_code=404)


# --- App Assembly ------------------------------------------------------------

# FastMCP 3.x: http_app() returns a StarletteWithLifespan object.
# Its lifespan MUST be delegated to the parent app to initialize the
# StreamableHTTPSessionManager task group. We wrap it with our Monarch init.
mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    # Delegate to FastMCP's own lifespan first (required for /mcp to work).
    async with mcp_asgi.lifespan(app):
        # Also try to init Monarch at startup; each handler retries lazily.
        try:
            await _init_monarch()
        except Exception as exc:
            logger.warning("Monarch init failed at startup (will retry on first request): %s", exc)
        yield


app = Starlette(
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        # REST endpoints for n8n
        Route("/api/accounts", endpoint=api_accounts, methods=["GET"]),
        Route("/api/transactions", endpoint=api_transactions, methods=["GET"]),
        Route("/api/cashflow", endpoint=api_cashflow, methods=["GET"]),
        Route("/api/budgets", endpoint=api_budgets, methods=["GET"]),
        Route("/api/recurring", endpoint=api_recurring, methods=["GET"]),
        Route("/api/networth", endpoint=api_networth, methods=["GET"]),
        Route("/api/transaction/{id:str}", endpoint=api_update_transaction, methods=["POST"]),
        Route("/api/token", endpoint=api_token, methods=["GET"]),
        # FastMCP MCP protocol - handles /mcp (catch-all after explicit routes)
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)

app.add_middleware(APIKeyMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
