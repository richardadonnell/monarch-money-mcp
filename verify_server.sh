#!/usr/bin/env bash
# Phase 2 verification for monarch-mcp (MIGRATION-fastmcp4.md).
#
# Usage: ./verify.sh [BASE_URL]          # default http://localhost:8000
#
# Reads MCP_API_KEY from the environment, else from the repo .env
# ($MONARCH_REPO/.env, defaulting to the directory holding this script).
# The key is never printed. Live financial data is never printed -- only counts.
#
# Exit 0 if every check passes, 1 otherwise.

set -uo pipefail

# Git Bash / MSYS mangles argv entries that look like unix paths. Header values
# such as "Mcp-Method: tools/call" must survive intact.
export MSYS2_ARG_CONV_EXCL='*'
export MSYS_NO_PATHCONV=1

BASE="${1:-http://localhost:8000}"
BASE="${BASE%/}"
# .env lives next to this script once it sits in the repo root; MONARCH_REPO
# overrides for running it from elsewhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${MONARCH_REPO:-$SCRIPT_DIR}"
# One per @mcp.tool decorator in server.py. Bump when a tool is added.
EXPECTED_TOOLS="${EXPECTED_TOOLS:-11}"
# One per @mcp.prompt decorator in server.py. Bump when a prompt is added.
EXPECTED_PROMPTS="${EXPECTED_PROMPTS:-5}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
if [ ! -t 1 ]; then RED=; GRN=; YEL=; DIM=; RST=; fi

TOTAL=0
FAILED=0
REST_ACCOUNTS=""
pass() { TOTAL=$((TOTAL+1)); printf '%sPASS%s  %s\n' "$GRN" "$RST" "$*"; }
fail() { TOTAL=$((TOTAL+1)); FAILED=$((FAILED+1)); printf '%sFAIL%s  %s\n' "$RED" "$RST" "$*"; }
info() { printf '      %s%s%s\n' "$DIM" "$*" "$RST"; }
head2() { printf '\n%s--- %s%s\n' "$YEL" "$*" "$RST"; }

# --- prerequisites ------------------------------------------------------------

command -v curl >/dev/null 2>&1 || { echo "curl not found"; exit 1; }
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null)"
HAVE_JQ=false
command -v jq >/dev/null 2>&1 && HAVE_JQ=true
# VERIFY_NO_JQ=1 forces the python parser, so the fallback path stays exercised.
[ -n "${VERIFY_NO_JQ:-}" ] && HAVE_JQ=false
if ! $HAVE_JQ && [ -z "$PY" ]; then echo "need jq or python for JSON parsing"; exit 1; fi

# curl here is a native Windows build, and MSYS argv conversion is disabled above,
# so hand it a Windows-style path. On Linux cygpath is absent and TMP stays POSIX.
TMPDIR_POSIX="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_POSIX"' EXIT
if command -v cygpath >/dev/null 2>&1; then
  TMP="$(cygpath -m "$TMPDIR_POSIX")"
else
  TMP="$TMPDIR_POSIX"
fi

# --- API key ------------------------------------------------------------------

if [ -z "${MCP_API_KEY:-}" ]; then
  ENVFILE="$REPO/.env"
  if [ -f "$ENVFILE" ]; then
    MCP_API_KEY="$(sed -n 's/^[[:space:]]*MCP_API_KEY[[:space:]]*=[[:space:]]*//p' "$ENVFILE" \
      | head -n1 | tr -d '\r' | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
  fi
fi
if [ -z "${MCP_API_KEY:-}" ]; then
  echo "MCP_API_KEY not set and not found in $REPO/.env"
  exit 1
fi
AUTH="Authorization: Bearer $MCP_API_KEY"

# --- JSON helpers -------------------------------------------------------------
# Responses may be SSE-framed ("event: message\ndata: {...}"). de_sse normalises
# any body to plain JSON before parsing.

PJ="$TMP/pj.py"
cat > "$PJ" <<'PYEOF'
import json, sys

op, path = sys.argv[1], sys.argv[2]
expr = sys.argv[3] if len(sys.argv) > 3 else ""
try:
    doc = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(1)

def dig(d, e):
    cur = d
    for part in [p for p in e.lstrip(".").split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

if op == "valid":
    sys.exit(0)
val = dig(doc, expr)
if op == "get":
    if val is None:
        sys.exit(0)
    print(val if isinstance(val, str) else json.dumps(val, separators=(",", ":")))
elif op == "count":
    print(len(val) if isinstance(val, (list, dict, str)) else 0)
elif op == "names":
    for item in val or []:
        if isinstance(item, dict) and "name" in item:
            print(item["name"])
elif op == "has":
    sys.exit(0 if val is not None else 1)
PYEOF

de_sse() { # de_sse <raw> <out>
  # curl writes no output file when it cannot connect.
  if [ ! -f "$1" ]; then : > "$2"; return; fi
  if grep -q '^data:' "$1" 2>/dev/null; then
    sed -n 's/^data:[[:space:]]*//p' "$1" | head -n1 > "$2"
  else
    cp "$1" "$2"
  fi
}

jvalid() { if $HAVE_JQ; then jq -e . "$1" >/dev/null 2>&1; else "$PY" "$PJ" valid "$1"; fi; }
jget()   { if $HAVE_JQ; then jq -rc "$2 // empty" "$1" 2>/dev/null; else "$PY" "$PJ" get "$1" "$2"; fi; }
jhas()   { if $HAVE_JQ; then jq -e "$2 != null" "$1" >/dev/null 2>&1; else "$PY" "$PJ" has "$1" "$2"; fi; }
jcount() { if $HAVE_JQ; then jq -r "($2 // []) | length" "$1" 2>/dev/null; else "$PY" "$PJ" count "$1" "$2"; fi; }
jnames() { if $HAVE_JQ; then jq -r "($2 // []) | .[]? | .name // empty" "$1" 2>/dev/null; else "$PY" "$PJ" names "$1" "$2"; fi; }

has_session_hdr() { grep -qi '^mcp-session-id:' "$1"; }
snippet() {
  [ -f "$1" ] || { printf '(no response -- could not connect)'; return; }
  tr -d '\r' < "$1" | head -c "${2:-200}" | tr '\n' ' '
}

# Modern-era (2026-07-28) requests must carry a params._meta envelope with
# io.modelcontextprotocol/protocolVersion + /clientCapabilities, and the
# MCP-Protocol-Version / Mcp-Method / Mcp-Name headers must agree with the body
# (mcp/shared/inbound.py validate ladder; a mismatch is -32020 HeaderMismatch).
#
# label -> files: $TMP/<label>.raw (body), .json (de-SSE'd), .hdr, .code
mcp_post() { # mcp_post <label> <body> [extra header]...
  local label="$1" body="$2"; shift 2
  local args=(-sS -o "$TMP/$label.raw" -D "$TMP/$label.hdr" -w '%{http_code}'
              -X POST "$BASE/mcp"
              -H "$AUTH"
              -H 'Content-Type: application/json'
              -H 'Accept: application/json, text/event-stream')
  local h; for h in "$@"; do args+=(-H "$h"); done
  args+=(--data-binary "$body")
  curl "${args[@]}" > "$TMP/$label.code" 2>"$TMP/$label.err" || echo 000 > "$TMP/$label.code"
  de_sse "$TMP/$label.raw" "$TMP/$label.json"
  cat "$TMP/$label.code"
}

echo "Phase 2 verification -- $BASE"
info "JSON parser: $($HAVE_JQ && echo jq || echo "python ($PY)")"

# --- 1. /health ---------------------------------------------------------------

head2 "1. GET /health (no auth -- route is exempt)"
code=$(curl -sS -o "$TMP/health.raw" -w '%{http_code}' "$BASE/health" 2>/dev/null) || code=000
de_sse "$TMP/health.raw" "$TMP/health.json"
if [ "$code" = "200" ]; then
  pass "/health -> 200 $(snippet "$TMP/health.json" 80)"
else
  fail "/health -> $code (expected 200)"
  info "$(snippet "$TMP/health.raw")"
fi

# --- 2. /api/accounts unauthenticated ----------------------------------------

head2 "2. GET /api/accounts without auth (APIKeyMiddleware live?)"
code=$(curl -sS -o "$TMP/noauth.raw" -w '%{http_code}' "$BASE/api/accounts" 2>/dev/null) || code=000
if [ "$code" = "401" ]; then
  pass "/api/accounts (no auth) -> 401"
else
  fail "/api/accounts (no auth) -> $code (expected 401)"
fi

# --- 3. /api/accounts authenticated ------------------------------------------

head2 "3. GET /api/accounts with Bearer key"
code=$(curl -sS -o "$TMP/rest.raw" -w '%{http_code}' -H "$AUTH" "$BASE/api/accounts" 2>/dev/null) || code=000
de_sse "$TMP/rest.raw" "$TMP/rest.json"
if [ "$code" != "200" ]; then
  fail "/api/accounts (authed) -> $code (expected 200)"
  info "$(snippet "$TMP/rest.raw")"
elif ! jvalid "$TMP/rest.json"; then
  fail "/api/accounts (authed) -> 200 but body is not valid JSON"
else
  REST_ACCOUNTS=$(jcount "$TMP/rest.json" '.accounts')
  pass "/api/accounts (authed) -> 200, JSON parsed (accounts: $REST_ACCOUNTS)"
fi

# --- 4. modern server/discover -----------------------------------------------

head2 "4. MODERN server/discover (MCP-Protocol-Version: 2026-07-28)"
code=$(mcp_post discover \
  '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"phase2-verify","version":"1.0"}}}}' \
  'MCP-Protocol-Version: 2026-07-28' \
  'Mcp-Method: server/discover')
SUPPORTED="$(jget "$TMP/discover.json" '.result.supportedVersions')"
[ -z "$SUPPORTED" ] && SUPPORTED="$(jget "$TMP/discover.json" '.supportedVersions')"
if [ "$code" != "200" ]; then
  fail "server/discover -> $code (expected 200)"
  info "$(snippet "$TMP/discover.raw")"
elif [ -z "$SUPPORTED" ]; then
  fail "server/discover -> 200 but no supportedVersions in the response"
  info "$(snippet "$TMP/discover.raw" 300)"
elif printf '%s' "$SUPPORTED" | grep -q '2026-07-28'; then
  pass "server/discover -> 200, supportedVersions: $SUPPORTED"
else
  fail "server/discover -> 200 but supportedVersions lacks 2026-07-28: $SUPPORTED"
fi
rt="$(jget "$TMP/discover.json" '.result.resultType')"
[ -n "$rt" ] && info "resultType: $rt"
scope="$(jget "$TMP/discover.json" '.result.cacheScope')"
if [ "$scope" = "private" ]; then
  pass "server/discover cacheScope: private"
else
  fail "server/discover cacheScope is '${scope:-<absent>}' (expected private)"
fi

# --- 5. modern tools/list -----------------------------------------------------

head2 "5. MODERN tools/list"
code=$(mcp_post toolslist \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"phase2-verify","version":"1.0"}}}}' \
  'MCP-Protocol-Version: 2026-07-28' \
  'Mcp-Method: tools/list')
if [ "$code" != "200" ]; then
  fail "tools/list -> $code (expected 200)"
  info "$(snippet "$TMP/toolslist.raw" 300)"
else
  names="$(jnames "$TMP/toolslist.json" '.result.tools')"
  count=$(printf '%s' "$names" | grep -c . || true)
  if [ "${count:-0}" = "$EXPECTED_TOOLS" ]; then
    pass "tools/list -> 200, $count tools (expected $EXPECTED_TOOLS)"
    info "$(printf '%s' "$names" | tr '\n' ' ')"
  elif [ "${count:-0}" -gt 0 ]; then
    fail "tools/list -> 200 but $count tools, expected $EXPECTED_TOOLS"
    info "$(printf '%s' "$names" | tr '\n' ' ')"
  else
    fail "tools/list -> 200 but no tools in .result.tools"
    info "$(snippet "$TMP/toolslist.raw" 300)"
  fi
fi
if jhas "$TMP/toolslist.json" '.result.resultType'; then
  pass "tools/list result carries resultType: $(jget "$TMP/toolslist.json" '.result.resultType')"
else
  fail "tools/list result is missing resultType (modern-era field)"
fi

# --- 5b. modern prompts/list --------------------------------------------------

head2 "5b. MODERN prompts/list"
code=$(mcp_post promptslist   '{"jsonrpc":"2.0","id":9,"method":"prompts/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"phase2-verify","version":"1.0"}}}}'   'MCP-Protocol-Version: 2026-07-28'   'Mcp-Method: prompts/list')
if [ "$code" != "200" ]; then
  fail "prompts/list -> $code (expected 200)"
  info "$(snippet "$TMP/promptslist.raw" 300)"
else
  pnames="$(jnames "$TMP/promptslist.json" '.result.prompts')"
  pcount=$(printf '%s' "$pnames" | grep -c . || true)
  if [ "${pcount:-0}" = "$EXPECTED_PROMPTS" ]; then
    pass "prompts/list -> 200, $pcount prompts (expected $EXPECTED_PROMPTS)"
    info "$(printf '%s' "$pnames" | tr '
' ' ')"
  elif [ "${pcount:-0}" -gt 0 ]; then
    fail "prompts/list -> 200 but $pcount prompts, expected $EXPECTED_PROMPTS"
    info "$(printf '%s' "$pnames" | tr '
' ' ')"
  else
    fail "prompts/list -> 200 but no prompts in .result.prompts"
    info "$(snippet "$TMP/promptslist.raw" 300)"
  fi
fi

# Render one prompt end to end. No Monarch call happens on this path -- a
# prompt returns text, so a failure here is a FastMCP wiring problem, not an
# upstream outage.
code=$(mcp_post promptget   '{"jsonrpc":"2.0","id":10,"method":"prompts/get","params":{"name":"monthly_review","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"phase2-verify","version":"1.0"}}}}'   'MCP-Protocol-Version: 2026-07-28'   'Mcp-Method: prompts/get'   'Mcp-Name: monthly_review')
if [ "$code" != "200" ]; then
  fail "prompts/get monthly_review -> $code (expected 200)"
  info "$(snippet "$TMP/promptget.raw" 300)"
elif jhas "$TMP/promptget.json" '.result.messages[0].content.text'; then
  pass "prompts/get monthly_review -> 200, rendered a message"
else
  fail "prompts/get monthly_review -> 200 but no .result.messages[0].content.text"
  info "$(snippet "$TMP/promptget.raw" 300)"
fi

# --- 6. no Mcp-Session-Id on modern responses --------------------------------

head2 "6. No Mcp-Session-Id header on modern responses (stateless era)"
leaked=""
for label in discover toolslist; do
  [ -f "$TMP/$label.hdr" ] && has_session_hdr "$TMP/$label.hdr" && leaked="$leaked $label"
done
if [ -z "$leaked" ]; then
  pass "no Mcp-Session-Id returned by server/discover or tools/list"
else
  fail "Mcp-Session-Id present on modern response(s):$leaked"
fi

# --- 7. modern tools/call get_accounts (live Monarch data) -------------------

head2 "7. MODERN tools/call get_accounts (live data; counts only, no values)"
code=$(mcp_post toolscall \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_accounts","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"phase2-verify","version":"1.0"}}}}' \
  'MCP-Protocol-Version: 2026-07-28' \
  'Mcp-Method: tools/call' \
  'Mcp-Name: get_accounts')
if [ "$code" != "200" ]; then
  fail "tools/call get_accounts -> $code (expected 200)"
  info "rpc error: $(jget "$TMP/toolscall.json" '.error.message') $(jget "$TMP/toolscall.json" '.error.code')"
else
  errcode="$(jget "$TMP/toolscall.json" '.error.code')"
  iserr="$(jget "$TMP/toolscall.json" '.result.isError')"
  if [ -n "$errcode" ]; then
    fail "tools/call get_accounts -> 200 but JSON-RPC error $errcode: $(jget "$TMP/toolscall.json" '.error.message')"
  elif [ "$iserr" = "true" ]; then
    fail "tools/call get_accounts -> 200 but isError=true"
  else
    # Count accounts without ever emitting names or balances.
    n=$("$PY" - "$TMP/toolscall.json" <<'PYEOF'
# Prints ONLY a record count. Account names and balances never leave this block.
import json, sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
res = doc.get("result", {}) or {}


def account_list(value, depth=0):
    """Dig out the accounts array. The tool returns a JSON *string*, so it shows
    up as content[].text and again as structuredContent.result -- both need a
    json.loads before the list is reachable."""
    if depth > 4:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("accounts", "result", "data", "items"):
            if key in value:
                found = account_list(value[key], depth + 1)
                if found is not None:
                    return found
    return None


candidates = [c.get("text") for c in res.get("content", []) or []
              if isinstance(c, dict) and c.get("type") == "text"]
candidates.append(res.get("structuredContent"))
for candidate in candidates:
    found = account_list(candidate)
    if found is not None:
        print(len(found))
        break
else:
    print(-1)
PYEOF
)
    if [ "${n:--1}" -gt 0 ] 2>/dev/null; then
      pass "tools/call get_accounts -> 200, isError=${iserr:-false}, $n accounts returned"
      # Same underlying call as check 3 -- the two counts should agree.
      if [ -n "${REST_ACCOUNTS:-}" ] && [ "$n" != "$REST_ACCOUNTS" ]; then
        info "note: /api/accounts reported $REST_ACCOUNTS accounts, MCP reported $n"
      fi
    else
      fail "tools/call get_accounts -> 200 but no account records parsed out of the result"
    fi
  fi
fi

# --- 7b. Mcp-Name must agree with the body -----------------------------------
# Rejected on the header rung of the inbound ladder, before dispatch -- the tool
# never runs, so this costs no Monarch call and touches no financial data.

head2 "7b. Mismatched Mcp-Name is refused (-32020 HeaderMismatch)"
code=$(mcp_post mismatch   '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_accounts","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'   'MCP-Protocol-Version: 2026-07-28'   'Mcp-Method: tools/call'   'Mcp-Name: not_the_right_tool')
mmcode="$(jget "$TMP/mismatch.json" '.error.code')"
if [ "$mmcode" = "-32020" ]; then
  pass "mismatched Mcp-Name -> HTTP $code, JSON-RPC -32020 HeaderMismatch"
else
  fail "mismatched Mcp-Name -> HTTP $code, error code ${mmcode:-<none>} (expected -32020)"
  info "$(snippet "$TMP/mismatch.raw" 200)"
fi


# --- 8. legacy handshake still works -----------------------------------------

head2 "8. LEGACY initialize (no MCP-Protocol-Version header)"
code=$(curl -sS -o "$TMP/legacy.raw" -D "$TMP/legacy.hdr" -w '%{http_code}' \
  -X POST "$BASE/mcp" \
  -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  2>/dev/null) || code=000
de_sse "$TMP/legacy.raw" "$TMP/legacy.json"
negotiated="$(jget "$TMP/legacy.json" '.result.protocolVersion')"
srvname="$(jget "$TMP/legacy.json" '.result.serverInfo.name')"
if [ "$code" != "200" ]; then
  fail "legacy initialize -> $code (expected 200)"
  info "$(snippet "$TMP/legacy.raw" 300)"
elif [ -z "$negotiated" ]; then
  fail "legacy initialize -> 200 but no .result.protocolVersion in the response"
  info "$(snippet "$TMP/legacy.raw" 300)"
else
  pass "legacy initialize -> 200, protocolVersion=$negotiated serverInfo.name=${srvname:-?}"
fi
# The legacy handshake is the stateful path: it must hand back a session id.
if has_session_hdr "$TMP/legacy.hdr"; then
  pass "legacy initialize returned an Mcp-Session-Id header (stateful path intact)"
else
  fail "legacy initialize returned no Mcp-Session-Id header"
fi

# --- summary ------------------------------------------------------------------

printf '\n%s/%s checks passed\n' "$((TOTAL-FAILED))" "$TOTAL"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
