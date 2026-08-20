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
  MONARCH_EMAIL      fallback: email for login, and for re-login after a 401
  MONARCH_PASSWORD   fallback: password for login, and for re-login after a 401
  MONARCH_MFA_SECRET TOTP secret key for 2FA accounts (Base32 seed, NOT the 6-digit code)
                     Found in: Monarch Settings -> Security -> MFA -> "Two-factor text code"
                     Or in 1Password: Edit entry -> OTP field -> Copy Secret Key
  MCP_API_KEY        required; protects all endpoints
  PORT               optional, defaults to 8000
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import uvicorn
from fastmcp import FastMCP
from gql import gql
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

MCP_API_KEY: str = os.environ["MCP_API_KEY"]  # required
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
_auth_epoch: int = 0  # bumped on every successful re-auth; dedupes concurrent retries
_auth_lock = asyncio.Lock()
# After a failed re-login, refuse further attempts until this monotonic deadline.
# ponytail: one flat cooldown, not exponential backoff -- Monarch either accepts
# the credentials or it does not, and a stuck deploy needs a human either way.
AUTH_COOLDOWN_SECONDS: int = 60
_auth_blocked_until: float = 0.0

# Fix: Monarch removed the legacy `goals` query from their GraphQL schema.
# monarchmoney v0.1.15 still emits `goals { id name completedAt targetDate }`
# inside the GetJointPlanningData query (guarded by `@include(if: $useLegacyGoals)`,
# but the server validates field selections regardless of @include). The validation
# crash surfaces as Monarch's generic "Something went wrong while processing"
# with locations: [{ line: 120, column: 5 }] (pointing at `goals.name`).
#
# Patch `mm.get_budgets` with an equivalent query that drops the legacy goals
# fragments. `goalsV2` (the current path, default use_v2_goals=True) is preserved.
_BUDGETS_QUERY = gql(
    """
      query GetJointPlanningData($startDate: Date!, $endDate: Date!, $useV2Goals: Boolean!) {
        budgetData(startMonth: $startDate, endMonth: $endDate) {
          monthlyAmountsByCategory {
            category { id __typename }
            monthlyAmounts {
              month
              plannedCashFlowAmount
              plannedSetAsideAmount
              actualAmount
              remainingAmount
              previousMonthRolloverAmount
              rolloverType
              __typename
            }
            __typename
          }
          monthlyAmountsByCategoryGroup {
            categoryGroup { id __typename }
            monthlyAmounts {
              month
              plannedCashFlowAmount
              actualAmount
              remainingAmount
              previousMonthRolloverAmount
              rolloverType
              __typename
            }
            __typename
          }
          monthlyAmountsForFlexExpense {
            budgetVariability
            monthlyAmounts {
              month
              plannedCashFlowAmount
              actualAmount
              remainingAmount
              previousMonthRolloverAmount
              rolloverType
              __typename
            }
            __typename
          }
          totalsByMonth {
            month
            totalIncome { plannedAmount actualAmount remainingAmount previousMonthRolloverAmount __typename }
            totalExpenses { plannedAmount actualAmount remainingAmount previousMonthRolloverAmount __typename }
            totalFixedExpenses { plannedAmount actualAmount remainingAmount previousMonthRolloverAmount __typename }
            totalNonMonthlyExpenses { plannedAmount actualAmount remainingAmount previousMonthRolloverAmount __typename }
            totalFlexibleExpenses { plannedAmount actualAmount remainingAmount previousMonthRolloverAmount __typename }
            __typename
          }
          __typename
        }
        categoryGroups {
          id
          name
          order
          groupLevelBudgetingEnabled
          budgetVariability
          rolloverPeriod { id startMonth endMonth __typename }
          categories {
            id
            name
            order
            budgetVariability
            rolloverPeriod { id startMonth endMonth __typename }
            __typename
          }
          type
          __typename
        }
        goalsV2 @include(if: $useV2Goals) {
          id
          name
          archivedAt
          completedAt
          priority
          imageStorageProvider
          imageStorageProviderId
          plannedContributions(startMonth: $startDate, endMonth: $endDate) { id month amount __typename }
          monthlyContributionSummaries(startMonth: $startDate, endMonth: $endDate) { month sum __typename }
          __typename
        }
        budgetSystem
      }
    """
)


async def _patched_get_budgets(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    use_legacy_goals: Optional[bool] = False,  # accepted for kwarg-compat, ignored
    use_v2_goals: Optional[bool] = True,
) -> dict[str, Any]:
    if bool(start_date) != bool(end_date):
        raise Exception(
            "You must specify both a startDate and endDate, not just one of them."
        )

    if not start_date and not end_date:
        today = datetime.today()
        last_month = today.month - 1
        last_month_year = today.year
        if last_month < 1:
            last_month_year -= 1
            last_month = 12
        start_date = datetime(last_month_year, last_month, 1).strftime("%Y-%m-%d")

        next_month = today.month + 1
        next_month_year = today.year
        if next_month > 12:
            next_month_year += 1
            next_month = 1
        last_day = calendar.monthrange(next_month_year, next_month)[1]
        end_date = datetime(next_month_year, next_month, last_day).strftime("%Y-%m-%d")

    return await mm.gql_call(
        operation="GetJointPlanningData",
        graphql_query=_BUDGETS_QUERY,
        variables={
            "startDate": start_date,
            "endDate": end_date,
            "useV2Goals": bool(use_v2_goals),
        },
    )


mm.get_budgets = _patched_get_budgets  # type: ignore[assignment]


async def _login() -> None:
    """Log in with email/password (+ auto-TOTP). Marks the client ready on success."""
    global _monarch_ready
    # monarchmoney POSTs the login with ClientSession(headers=self._headers), so a
    # stale "Authorization: Token ..." left by the MONARCH_TOKEN branch travels on
    # the login request and Monarch rejects it 401 without ever reading the
    # credentials. Clear it first; _login_user re-sets it from the fresh token.
    mm._headers.pop("Authorization", None)
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
            logger.info(
                "Monarch: 2FA TOTP generated automatically from MONARCH_MFA_SECRET"
            )
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

    await _login()


def _is_auth_error(exc: BaseException) -> bool:
    """True when Monarch rejected the session itself (HTTP 401), not the query."""
    # gql's TransportServerError carries .code; aiohttp's ClientResponseError carries .status
    return getattr(exc, "code", None) == 401 or getattr(exc, "status", None) == 401


async def _reauth(seen_epoch: int) -> bool:
    """
    Re-login after a 401. Returns False when there is nothing to re-mint: a
    token-only deploy has no credentials, so retrying would just replay the
    same dead token.

    Callers that saw the same epoch collapse into a single login. That matters
    because when a session expires every in-flight request 401s at once, and a
    TOTP code cannot be spent twice.

    A login that fails starts a cooldown: without it every incoming request
    attempts its own login, which is a credential-stuffing pattern aimed at
    Monarch and a good way to get the account rate-limited or locked.
    """
    global _auth_epoch, _auth_blocked_until
    if not (MONARCH_EMAIL and MONARCH_PASSWORD):
        return False
    async with _auth_lock:
        if _auth_epoch != seen_epoch:
            return True  # another task already re-authenticated

        remaining = _auth_blocked_until - time.monotonic()
        if remaining > 0:
            logger.warning(
                "Monarch re-auth in cooldown for another %.0fs after a failed "
                "login - not retrying",
                remaining,
            )
            return False

        try:
            await _login()
        except Exception:
            _auth_blocked_until = time.monotonic() + AUTH_COOLDOWN_SECONDS
            logger.error(
                "Monarch re-login failed - backing off for %ds",
                AUTH_COOLDOWN_SECONDS,
            )
            raise
        _auth_blocked_until = 0.0
        _auth_epoch += 1
    return True


async def _call(fn, *args: Any, **kwargs: Any) -> Any:
    """
    Run a Monarch client call, re-authenticating once if the session expired.

    An expired MONARCH_TOKEN is promoted to a credential login here, so a stale
    token self-heals at runtime instead of 401ing until the container restarts.
    """
    await _init_monarch()
    seen_epoch = _auth_epoch
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:
        if not _is_auth_error(exc):
            raise
        logger.warning("Monarch rejected the session (401) - re-authenticating")
        if not await _reauth(seen_epoch):
            raise
        # ponytail: one retry, then give up. A second 401 is a real auth failure
        # (wrong password, revoked MFA), not an expired session.
        return await fn(*args, **kwargs)


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
    data = await _call(mm.get_accounts)
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
    data = await _call(mm.get_transactions, **kwargs)
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
    data = await _call(mm.get_cashflow_summary, **kwargs)
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
    data = await _call(mm.get_cashflow, **kwargs)
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
    data = await _call(mm.get_budgets, **kwargs)
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
    data = await _call(mm.get_recurring_transactions, **kwargs)
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
    data = await _call(mm.get_account_holdings, account_id)
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
    data = await _call(mm.get_aggregate_snapshots, **kwargs)
    return _json(data)


@mcp.tool(
    name="get_transaction_categories",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_transaction_categories() -> str:
    """Return all transaction categories with IDs. Use IDs with get_transactions filters or set_budget_amount."""
    data = await _call(mm.get_transaction_categories)
    return _json(data)


@mcp.tool(
    name="update_transaction",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
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
    kwargs: dict[str, Any] = {"transaction_id": transaction_id}
    for k, v in {
        "category_id": category_id,
        "notes": notes,
        "hide_from_reports": hide_from_reports,
        "needs_review": needs_review,
    }.items():
        if v is not None:
            kwargs[k] = v
    data = await _call(mm.update_transaction, **kwargs)
    return _json(data)


@mcp.tool(
    name="set_budget_amount",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
    },
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
    data = await _call(
        mm.set_budget_amount,
        amount=amount,
        category_id=category_id,
        start_date=start_date,
    )
    return _json(data)


# --- MCP Prompts -------------------------------------------------------------
# Reusable workflows served over prompts/list. Claude Code surfaces them as
# /mcp__monarch-money__<name>. Prompt arguments cross the wire as strings, so
# every parameter is typed str and interpolated into the message text -- no
# coercion to gamble on, and no Monarch call happens here. The returned string
# becomes a user message; the model then picks the tools.


@mcp.prompt(name="monthly_review")
def monthly_review(month: str = "") -> str:
    """Review a month's spending against budget and flag anomalies."""
    period = month or "the current calendar month"
    return (
        f"Review my Monarch Money spending for {period}.\n\n"
        "1. Call get_cashflow_summary for the period to get income, expenses, and savings rate.\n"
        "2. Call get_cashflow for the per-category breakdown.\n"
        "3. Call get_budgets for the same period and compare actual vs planned per category.\n"
        "4. Call get_transactions (limit 500) and surface the largest individual charges.\n\n"
        "Report: savings rate, the three categories most over budget, the three largest "
        "one-off transactions, and anything that looks like a duplicate or an unexpected charge. "
        "Use ISO 8601 dates (YYYY-MM-DD) on every call."
    )


@mcp.prompt(name="find_subscriptions")
def find_subscriptions(months: str = "3") -> str:
    """Audit recurring charges and list cancellation candidates."""
    return (
        f"Audit my recurring Monarch Money charges over the last {months} months.\n\n"
        "1. Call get_recurring_transactions for that window.\n"
        "2. Call get_transactions with is_recurring=true over the same window to catch "
        "anything the recurring feed missed.\n\n"
        "Report every subscription with its cadence, amount, and annualized cost, sorted by "
        "annual spend. Call out price increases versus earlier months, near-duplicate services, "
        "and anything charged but seemingly unused. Do not cancel or modify anything."
    )


@mcp.prompt(name="budget_variance")
def budget_variance(month: str = "") -> str:
    """Show which budget categories are over or under, and by how much."""
    period = month or "the current calendar month"
    return (
        f"Show my Monarch Money budget variance for {period}.\n\n"
        "Call get_budgets for the period, then produce a table of category, planned, actual, "
        "variance in dollars, and variance as a percent -- sorted worst overspend first. "
        "Total the overspend and the underspend separately. For the worst three categories, "
        "call get_transactions filtered to that category_id to name the transactions driving it."
    )


@mcp.prompt(name="net_worth_check")
def net_worth_check(months: str = "12") -> str:
    """Summarize the net worth trend and what moved it."""
    return (
        f"Summarize my Monarch Money net worth trend over the last {months} months.\n\n"
        "1. Call get_net_worth_history for that window.\n"
        "2. Call get_accounts for the current per-account balances and types.\n\n"
        "Report the start value, current value, absolute and percent change, the average monthly "
        "change, and the best and worst months. Then state which accounts contributed most to the "
        "change, separating market movement from contributions where the data allows."
    )


@mcp.prompt(name="review_uncategorized")
def review_uncategorized(limit: str = "25") -> str:
    """Find transactions needing review and propose categories before applying them."""
    return (
        f"Help me clean up my Monarch Money transactions. Pull the most recent {limit} "
        "transactions with get_transactions.\n\n"
        "1. Call get_transaction_categories first so you have the category IDs.\n"
        "2. Identify transactions that are uncategorized, miscategorized, or flagged "
        "needs_review.\n"
        "3. Propose a category for each one, with the merchant name and amount as justification.\n\n"
        "Show me the full proposed list and wait for my explicit approval before calling "
        "update_transaction. Never write a change I have not approved."
    )


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
        return JSONResponse(await _call(mm.get_accounts))
    except Exception as exc:
        logger.error("api_accounts: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_transactions(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        limit = int(params.pop("limit", 100))
        return JSONResponse(await _call(mm.get_transactions, limit=limit, **params))
    except Exception as exc:
        logger.error("api_transactions: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_cashflow(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        return JSONResponse(await _call(mm.get_cashflow, **params))
    except Exception as exc:
        logger.error("api_cashflow: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_budgets(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        return JSONResponse(await _call(mm.get_budgets, **params))
    except Exception as exc:
        logger.error("api_budgets: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_recurring(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        return JSONResponse(await _call(mm.get_recurring_transactions, **params))
    except Exception as exc:
        logger.error("api_recurring: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_networth(request: Request) -> JSONResponse:
    try:
        params = dict(request.query_params)
        return JSONResponse(await _call(mm.get_aggregate_snapshots, **params))
    except Exception as exc:
        logger.error("api_networth: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_transaction(request: Request) -> JSONResponse:
    try:
        txn_id = request.path_params["id"]
        body = await request.json()
        data = await _call(mm.update_transaction, transaction_id=txn_id, **body)
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
            logger.warning(
                "Monarch init failed at startup (will retry on first request): %s", exc
            )
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
        Route(
            "/api/transaction/{id:str}",
            endpoint=api_update_transaction,
            methods=["POST"],
        ),
        Route("/api/token", endpoint=api_token, methods=["GET"]),
        # FastMCP MCP protocol - handles /mcp (catch-all after explicit routes)
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)

app.add_middleware(APIKeyMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
