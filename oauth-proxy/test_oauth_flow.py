"""Test end-to-end du flow OAuth 2.1 AGEA. A executer sur le VPS.
Necessite que OAUTH_ADMIN_APPROVAL_TOKEN soit dans l'environnement.
"""

import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("TEST_HOST", "srv987452.hstgr.cloud")
BASE = f"https://{HOST}/mcp-oauth"
WK_AS = f"https://{HOST}/.well-known/oauth-authorization-server"
WK_RS = f"https://{HOST}/.well-known/oauth-protected-resource"
APPR = os.environ["OAUTH_ADMIN_APPROVAL_TOKEN"]
REDIRECT = "http://localhost:12345/cb"
ctx = ssl.create_default_context()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(
    _NoRedirect(),
    urllib.request.HTTPSHandler(context=ctx),
)


def req(method, url, data=None, headers=None):
    hdrs = dict(headers or {})
    body = None
    if data is not None:
        if isinstance(data, dict):
            if hdrs.get("Content-Type") == "application/json":
                body = json.dumps(data).encode()
            else:
                body = urllib.parse.urlencode(data).encode()
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data
    r = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def require(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


print("=== Step 0: discovery ===")
s, _, b = req("GET", WK_AS)
print(f"AS discovery: HTTP {s}")
require(s == 200, b)
as_meta = json.loads(b)
print(f'  issuer: {as_meta["issuer"]}')
s, _, b = req("GET", WK_RS)
print(f"RS discovery: HTTP {s}")
require(s == 200, b)

print("\n=== Step 1: Dynamic Client Registration ===")
s, _, b = req(
    "POST",
    as_meta["registration_endpoint"],
    {
        "client_name": "test-client-oauth-flow",
        "redirect_uris": [REDIRECT],
        "scope": "read",
        "token_endpoint_auth_method": "none",
    },
    headers={"Content-Type": "application/json"},
)
print(f"register: HTTP {s}")
require(s == 201, b)
reg = json.loads(b)
CLIENT_ID = reg["client_id"]
print(f"  client_id: {CLIENT_ID}")

verifier = secrets.token_urlsafe(32)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
print(f"\n=== Step 2: PKCE ===")
print(f"  verifier_len={len(verifier)} challenge_len={len(challenge)}")

print("\n=== Step 3: GET /authorize (consent page) ===")
q = urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
)
s, _, b = req("GET", f"{BASE}/authorize?{q}")
print(f"  HTTP {s}, has approval_token field: {b.count(b'approval_token') > 0}")
require(s == 200, "authorize GET failed")

print("\n=== Step 4: POST /authorize (decision=allow + approval_token) ===")
s, hdrs, b = req(
    "POST",
    f"{BASE}/authorize",
    {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "decision": "allow",
        "approval_token": APPR,
    },
)
print(f"  HTTP {s}")
loc = hdrs.get("Location", "") or hdrs.get("location", "")
print(f"  Location: {loc[:80]}")
require(s == 302, "authorize POST failed")
qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
code = qs.get("code", [""])[0]
got_state = qs.get("state", [""])[0]
require(bool(code), "no code in redirect")
require(got_state == state, f"state mismatch: {got_state} vs {state}")
print(f"  code[:12]: {code[:12]}")

print("\n=== Step 5: POST /token (code exchange + PKCE verifier) ===")
s, _, b = req(
    "POST",
    f"{BASE}/token",
    {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    },
)
print(f"  HTTP {s}")
require(s == 200, b)
tok = json.loads(b)
ACCESS = tok["access_token"]
REFRESH = tok["refresh_token"]
print(f'  token_type={tok["token_type"]} expires_in={tok["expires_in"]} scope={tok["scope"]}')

print("\n=== Step 6: POST /mcp-oauth/mcp tools/list (Bearer) ===")
s, _, b = req(
    "POST",
    f"{BASE}/mcp",
    data=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode(),
    headers={
        "Authorization": f"Bearer {ACCESS}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
)
print(f"  HTTP {s}")
require(s == 200, b)
data = json.loads(b)
tools = [t["name"] for t in data["result"]["tools"]]
print(f"  tools received ({len(tools)}): {tools}")
READ_EXPECTED = {
    "search_memory",
    "search_facts",
    "get_entity",
    "get_history",
    "search_decisions",
    "search_legal",
    "search_jurisprudence",
    "search_admin_jurisprudence",
    "veille_juridique",
}
got = set(tools)
require(got.issubset(READ_EXPECTED), f"leaked tools: {got - READ_EXPECTED}")
require("save_memory" not in got, "save_memory should not appear with read scope")
print("  filtering OK: save_memory / correct_fact / lexia_alert filtered out")

print("\n=== Step 7: tools/call save_memory with read scope must be 403 ===")
s, _, b = req(
    "POST",
    f"{BASE}/mcp",
    data=json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "save_memory", "arguments": {"content": "test"}},
            "id": 2,
        }
    ).encode(),
    headers={
        "Authorization": f"Bearer {ACCESS}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
)
print(f"  HTTP {s}")
require(s == 403, f"expected 403 but got {s}: {b}")
print("  OK: tools/call save_memory rejected 403 insufficient_scope")

print("\n=== Step 8: refresh_token rotation ===")
s, _, b = req(
    "POST",
    f"{BASE}/token",
    {"grant_type": "refresh_token", "refresh_token": REFRESH, "client_id": CLIENT_ID},
)
print(f"  HTTP {s}")
require(s == 200, b)
tok2 = json.loads(b)
require(tok2["access_token"] != ACCESS, "new access_token should differ")
print("  new pair issued")

print("\n=== Step 9: old refresh_token rejected after rotation ===")
s, _, b = req(
    "POST",
    f"{BASE}/token",
    {"grant_type": "refresh_token", "refresh_token": REFRESH, "client_id": CLIENT_ID},
)
print(f"  HTTP {s} (expected 400)")
require(s == 400, b)

print("\n=== Step 10: revoke the new access_token ===")
s, _, b = req("POST", f"{BASE}/revoke", {"token": tok2["access_token"], "client_id": CLIENT_ID})
print(f"  HTTP {s}")
require(s == 200, b)

print("\n=== Step 11: MCP call with revoked token must be 401 ===")
s, _, b = req(
    "POST",
    f"{BASE}/mcp",
    data=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 3}).encode(),
    headers={
        "Authorization": f'Bearer {tok2["access_token"]}',
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    },
)
print(f"  HTTP {s} (expected 401)")
require(s == 401, b)

print("\n=== Step 12: scope escalation protection (18bis) ===")
# Register un client avec scope=read uniquement
s, _, b = req(
    "POST",
    as_meta["registration_endpoint"],
    {
        "client_name": "test-client-scope-subset",
        "redirect_uris": [REDIRECT],
        "scope": "read",
        "token_endpoint_auth_method": "none",
    },
    headers={"Content-Type": "application/json"},
)
require(s == 201, b)
SUBSET_CLIENT = json.loads(b)["client_id"]
print(f"  registered client with scope=read: {SUBSET_CLIENT}")

verifier2 = secrets.token_urlsafe(32)
challenge2 = base64.urlsafe_b64encode(hashlib.sha256(verifier2.encode()).digest()).rstrip(b"=").decode()

# T2: DCR scope=read + /authorize scope=read+write+admin -> 400 invalid_scope
print("  T2 - GET /authorize?scope=read+write+admin (escalation tentative)")
q_esc = urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": SUBSET_CLIENT,
        "redirect_uri": REDIRECT,
        "scope": "read write admin",
        "state": "t2",
        "code_challenge": challenge2,
        "code_challenge_method": "S256",
    }
)
s, _, b = req("GET", f"{BASE}/authorize?{q_esc}")
print(f"    HTTP {s} (expected 400)")
require(s == 400, f"T2 escalation should be blocked, got {s}: {b}")
require(b"invalid_scope" in b, f"T2 missing invalid_scope error: {b}")
print("  T2 OK: scope escalation blocked at GET /authorize")

# T2bis: meme tentative au POST /authorize (defense in depth)
print("  T2bis - POST /authorize?scope=read+write+admin direct (bypass GET)")
s, _, b = req(
    "POST",
    f"{BASE}/authorize",
    {
        "client_id": SUBSET_CLIENT,
        "redirect_uri": REDIRECT,
        "scope": "read write admin",
        "state": "t2bis",
        "code_challenge": challenge2,
        "code_challenge_method": "S256",
        "decision": "allow",
        "approval_token": APPR,
    },
)
print(f"    HTTP {s} (expected 400)")
require(s == 400, f"T2bis defense-in-depth failed, got {s}: {b}")
print("  T2bis OK: scope escalation blocked at POST /authorize")

# T3: DCR scope=read+write + /authorize scope=read seul -> 200 (subset valide)
s, _, b = req(
    "POST",
    as_meta["registration_endpoint"],
    {
        "client_name": "test-client-subset-rw",
        "redirect_uris": [REDIRECT],
        "scope": "read write",
        "token_endpoint_auth_method": "none",
    },
    headers={"Content-Type": "application/json"},
)
require(s == 201, b)
SUBSET_RW = json.loads(b)["client_id"]
print(f"  T3 - registered client scope=read+write: {SUBSET_RW}")

q_rw = urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": SUBSET_RW,
        "redirect_uri": REDIRECT,
        "scope": "read",
        "state": "t3",
        "code_challenge": challenge2,
        "code_challenge_method": "S256",
    }
)
s, _, b = req("GET", f"{BASE}/authorize?{q_rw}")
print(f"    HTTP {s} (expected 200)")
require(s == 200, f"T3 valid subset should pass, got {s}: {b}")
print("  T3 OK: scope subset (read of read+write) accepted")

# T4: DCR scope=read + /authorize scope vide -> 200 (fallback default 'read')
q_empty = urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": SUBSET_CLIENT,
        "redirect_uri": REDIRECT,
        "scope": "",
        "state": "t4",
        "code_challenge": challenge2,
        "code_challenge_method": "S256",
    }
)
s, _, b = req("GET", f"{BASE}/authorize?{q_empty}")
print(f"  T4 - empty scope HTTP {s} (expected 200 fallback 'read')")
require(s == 200, f"T4 empty scope should default to read, got {s}: {b}")
print("  T4 OK: empty scope defaults gracefully")

print("\n=== ALL 12 STEPS OK - OAuth 2.1 flow + 18bis scope subset validated ===")
