"""
OAuth 2.1 proxy devant le serveur MCP AGEA.
Ajoute OAuth 2.1 + DCR (RFC 7591) + PKCE (RFC 7636) + scopes, sans toucher mcp-remote-server.py.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
ISSUER = os.environ["OAUTH_ISSUER"].rstrip("/")
PUBLIC_BASE = ISSUER.rsplit("/mcp-oauth", 1)[0]
JWT_SECRET = os.environ["OAUTH_JWT_SECRET"]
ACCESS_TTL = int(os.environ.get("OAUTH_ACCESS_TOKEN_TTL", "3600"))
REFRESH_TTL = int(os.environ.get("OAUTH_REFRESH_TOKEN_TTL", "2592000"))
AUTH_CODE_TTL = int(os.environ.get("OAUTH_AUTH_CODE_TTL", "600"))
MCP_INTERNAL_URL = os.environ.get("MCP_INTERNAL_URL", "http://mcp-remote:8888/mcp")
ADMIN_APPROVAL_TOKEN = os.environ.get("OAUTH_ADMIN_APPROVAL_TOKEN", "")
DB_PATH = os.environ.get("OAUTH_DB_PATH", "/data/oauth.db")
AUDIT_LOG_PATH = os.environ.get("OAUTH_AUDIT_LOG", "/data/audit.log")

# Mapping scope -> liste des MCP tools autorises
SCOPE_TOOLS: dict[str, set[str]] = {
    "read": {
        "search_memory",
        "search_facts",
        "get_entity",
        "get_history",
        "search_decisions",
        "search_legal",
        "search_jurisprudence",
        "search_admin_jurisprudence",
        "veille_juridique",
    },
    "write": {
        "search_memory",
        "search_facts",
        "get_entity",
        "get_history",
        "search_decisions",
        "search_legal",
        "search_jurisprudence",
        "search_admin_jurisprudence",
        "veille_juridique",
        "save_memory",
        "correct_fact",
        "lexia_alert",
    },
    "admin": set(),  # vide = pas de filtrage (tout passe)
}
SUPPORTED_SCOPES = list(SCOPE_TOOLS.keys())

# -------------------------------------------------------------------
# Logging + audit
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("oauth-proxy")

Path(AUDIT_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)


def audit(event: str, **kw: Any) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [{event}] " + " ".join(
        f"{k}={v}" for k, v in kw.items()
    )
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("audit write failed: %s", e)
    log.info(line)


# -------------------------------------------------------------------
# SQLite
# -------------------------------------------------------------------
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                client_secret_hash TEXT,
                client_name TEXT,
                redirect_uris TEXT NOT NULL,
                scopes TEXT NOT NULL,
                token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
                created_at INTEGER NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                scope TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                code_challenge_method TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,              -- 'access' or 'refresh'
                client_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                parent_refresh_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens(client_id);
            CREATE INDEX IF NOT EXISTS idx_codes_client ON auth_codes(client_id);
            """
        )


_init_db()


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


# -------------------------------------------------------------------
# Crypto helpers
# -------------------------------------------------------------------
def _hash(value: str) -> str:
    return hmac.new(JWT_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def _rand(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if method == "S256":
        digest = hashlib.sha256(verifier.encode()).digest()
        import base64

        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return hmac.compare_digest(computed, challenge)
    if method == "plain":
        return hmac.compare_digest(verifier, challenge)
    return False


# -------------------------------------------------------------------
# FastAPI app + rate limit
# -------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AGEA OAuth 2.1 proxy", docs_url=None, redoc_url=None)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> Response:
    return JSONResponse(
        {"error": "rate_limit_exceeded", "detail": str(exc.detail)},
        status_code=429,
    )


templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------
@app.get("/healthz")
@app.get("/mcp-oauth/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# -------------------------------------------------------------------
# Discovery (.well-known) - RFC 8414
# -------------------------------------------------------------------
def _as_metadata() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "registration_endpoint": f"{ISSUER}/register",
        "revocation_endpoint": f"{ISSUER}/revoke",
        "scopes_supported": SUPPORTED_SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    }


def _rs_metadata() -> dict[str, Any]:
    return {
        "resource": f"{ISSUER}/mcp",
        "authorization_servers": [ISSUER],
        "scopes_supported": SUPPORTED_SCOPES,
        "bearer_methods_supported": ["header"],
    }


@app.get("/.well-known/oauth-authorization-server")
@app.get("/mcp-oauth/.well-known/oauth-authorization-server")
async def as_discovery() -> JSONResponse:
    return JSONResponse(_as_metadata())


@app.get("/.well-known/oauth-protected-resource")
@app.get("/mcp-oauth/.well-known/oauth-protected-resource")
async def rs_discovery() -> JSONResponse:
    return JSONResponse(_rs_metadata())


# Alias OIDC pour clients hybrides (Anthropic MCP) qui exigent ce path
# avant de fallback sur oauth-authorization-server. Retourne le metadata AS.
@app.get("/.well-known/openid-configuration")
@app.get("/mcp-oauth/.well-known/openid-configuration")
async def oidc_discovery() -> JSONResponse:
    return JSONResponse(_as_metadata())


# -------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# -------------------------------------------------------------------
class RegisterRequest(BaseModel):
    client_name: str | None = Field(default=None, max_length=256)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    scope: str | None = None
    token_endpoint_auth_method: str | None = None


@app.post("/mcp-oauth/register")
@limiter.limit("10/minute")
async def register(request: Request, body: RegisterRequest) -> JSONResponse:
    for uri in body.redirect_uris:
        if not (uri.startswith("https://") or uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")):
            raise HTTPException(400, {"error": "invalid_redirect_uri"})

    requested_scope = (body.scope or "read").strip()
    scope_tokens = [s for s in requested_scope.split() if s]
    for s in scope_tokens:
        if s not in SUPPORTED_SCOPES:
            raise HTTPException(400, {"error": "invalid_scope", "scope": s})
    effective_scope = " ".join(scope_tokens) if scope_tokens else "read"

    auth_method = body.token_endpoint_auth_method or "none"
    if auth_method not in {"none", "client_secret_post"}:
        raise HTTPException(400, {"error": "invalid_client_metadata"})

    client_id = "c_" + _rand(16)
    client_secret = None
    client_secret_hash = None
    if auth_method == "client_secret_post":
        client_secret = _rand(32)
        client_secret_hash = _hash(client_secret)

    with db() as con:
        con.execute(
            "INSERT INTO clients(client_id, client_secret_hash, client_name, redirect_uris, scopes, "
            "token_endpoint_auth_method, created_at, approved) VALUES(?,?,?,?,?,?,?,0)",
            (
                client_id,
                client_secret_hash,
                body.client_name or "",
                json.dumps(body.redirect_uris),
                effective_scope,
                auth_method,
                int(time.time()),
            ),
        )

    audit(
        "client_registered",
        client_id=client_id,
        name=(body.client_name or "").replace(" ", "_"),
        ip=get_remote_address(request),
        scope=effective_scope,
    )

    resp: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
        "scope": effective_scope,
    }
    if client_secret:
        resp["client_secret"] = client_secret
    return JSONResponse(resp, status_code=201)


# -------------------------------------------------------------------
# Authorize (consentement Mehdi)
# -------------------------------------------------------------------
@app.get("/mcp-oauth/authorize")
async def authorize_get(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("read"),
    state: str = Query(""),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
) -> HTMLResponse:
    if response_type != "code":
        raise HTTPException(400, {"error": "unsupported_response_type"})
    if code_challenge_method not in {"S256"}:
        raise HTTPException(400, {"error": "invalid_request", "detail": "code_challenge_method must be S256"})

    with db() as con:
        row = con.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
    if not row:
        raise HTTPException(400, {"error": "invalid_client"})
    allowed_uris = json.loads(row["redirect_uris"])
    if redirect_uri not in allowed_uris:
        raise HTTPException(400, {"error": "invalid_redirect_uri"})

    scope_tokens = [s for s in scope.split() if s]
    for s in scope_tokens:
        if s not in SUPPORTED_SCOPES:
            raise HTTPException(400, {"error": "invalid_scope", "scope": s})

    return templates.TemplateResponse(
        "consent.html",
        {
            "request": request,
            "client_name": row["client_name"] or client_id,
            "client_id": client_id,
            "scope": " ".join(scope_tokens) or "read",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
    )


@app.post("/mcp-oauth/authorize")
@limiter.limit("30/minute")
async def authorize_post(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form("read"),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    decision: str = Form(...),
    approval_token: str = Form(""),
) -> RedirectResponse:
    if ADMIN_APPROVAL_TOKEN and not hmac.compare_digest(approval_token, ADMIN_APPROVAL_TOKEN):
        raise HTTPException(403, {"error": "forbidden", "detail": "approval token invalid"})

    if decision != "allow":
        params = {"error": "access_denied"}
        if state:
            params["state"] = state
        audit("consent_denied", client_id=client_id, ip=get_remote_address(request))
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    with db() as con:
        row = con.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not row or redirect_uri not in json.loads(row["redirect_uris"]):
            raise HTTPException(400, {"error": "invalid_client_or_redirect"})
        code = _rand(32)
        con.execute(
            "INSERT INTO auth_codes(code_hash, client_id, redirect_uri, scope, code_challenge, "
            "code_challenge_method, expires_at, consumed) VALUES(?,?,?,?,?,?,?,0)",
            (
                _hash(code),
                client_id,
                redirect_uri,
                scope,
                code_challenge,
                code_challenge_method,
                int(time.time()) + AUTH_CODE_TTL,
            ),
        )
        con.execute("UPDATE clients SET approved=1 WHERE client_id=?", (client_id,))

    audit("consent_granted", client_id=client_id, scope=scope, ip=get_remote_address(request))
    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


# -------------------------------------------------------------------
# Token endpoint
# -------------------------------------------------------------------
def _issue_tokens(con: sqlite3.Connection, client_id: str, scope: str) -> dict[str, Any]:
    access = _rand(32)
    refresh = _rand(48)
    now = int(time.time())
    con.execute(
        "INSERT INTO tokens(token_hash, kind, client_id, scope, expires_at) VALUES(?,?,?,?,?)",
        (_hash(access), "access", client_id, scope, now + ACCESS_TTL),
    )
    con.execute(
        "INSERT INTO tokens(token_hash, kind, client_id, scope, expires_at) VALUES(?,?,?,?,?)",
        (_hash(refresh), "refresh", client_id, scope, now + REFRESH_TTL),
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


@app.post("/mcp-oauth/token")
@limiter.limit("30/minute")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: str = Form(None),
    client_secret: str = Form(None),
    code_verifier: str = Form(None),
    refresh_token: str = Form(None),
) -> JSONResponse:
    if grant_type == "authorization_code":
        if not all([code, redirect_uri, client_id, code_verifier]):
            raise HTTPException(400, {"error": "invalid_request"})
        with db() as con:
            client = con.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not client:
                raise HTTPException(400, {"error": "invalid_client"})
            if client["token_endpoint_auth_method"] == "client_secret_post":
                if not client_secret or not hmac.compare_digest(
                    _hash(client_secret), client["client_secret_hash"]
                ):
                    raise HTTPException(401, {"error": "invalid_client"})
            row = con.execute(
                "SELECT * FROM auth_codes WHERE code_hash=?", (_hash(code),)
            ).fetchone()
            if not row or row["consumed"] or row["expires_at"] < int(time.time()):
                raise HTTPException(400, {"error": "invalid_grant"})
            if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
                raise HTTPException(400, {"error": "invalid_grant"})
            if not _verify_pkce(code_verifier, row["code_challenge"], row["code_challenge_method"]):
                raise HTTPException(400, {"error": "invalid_grant", "detail": "pkce mismatch"})
            con.execute("UPDATE auth_codes SET consumed=1 WHERE code_hash=?", (_hash(code),))
            tokens = _issue_tokens(con, client_id, row["scope"])
        audit("token_issued", client_id=client_id, grant="code", scope=tokens["scope"], ip=get_remote_address(request))
        return JSONResponse(tokens)

    if grant_type == "refresh_token":
        if not refresh_token or not client_id:
            raise HTTPException(400, {"error": "invalid_request"})
        with db() as con:
            client = con.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not client:
                raise HTTPException(400, {"error": "invalid_client"})
            if client["token_endpoint_auth_method"] == "client_secret_post":
                if not client_secret or not hmac.compare_digest(
                    _hash(client_secret), client["client_secret_hash"]
                ):
                    raise HTTPException(401, {"error": "invalid_client"})
            row = con.execute(
                "SELECT * FROM tokens WHERE token_hash=? AND kind='refresh'", (_hash(refresh_token),)
            ).fetchone()
            if not row or row["revoked"] or row["expires_at"] < int(time.time()) or row["client_id"] != client_id:
                raise HTTPException(400, {"error": "invalid_grant"})
            # rotate : revoke old refresh, issue new pair
            con.execute("UPDATE tokens SET revoked=1 WHERE token_hash=?", (_hash(refresh_token),))
            tokens = _issue_tokens(con, client_id, row["scope"])
        audit("token_refreshed", client_id=client_id, scope=tokens["scope"], ip=get_remote_address(request))
        return JSONResponse(tokens)

    raise HTTPException(400, {"error": "unsupported_grant_type"})


# -------------------------------------------------------------------
# Revoke endpoint (RFC 7009)
# -------------------------------------------------------------------
@app.post("/mcp-oauth/revoke")
@limiter.limit("30/minute")
async def revoke(
    request: Request,
    token: str = Form(...),
    client_id: str = Form(None),
    client_secret: str = Form(None),
) -> Response:
    with db() as con:
        row = con.execute(
            "SELECT * FROM tokens WHERE token_hash=?", (_hash(token),)
        ).fetchone()
        if row:
            if client_id and row["client_id"] != client_id:
                return Response(status_code=200)
            con.execute("UPDATE tokens SET revoked=1 WHERE token_hash=?", (_hash(token),))
            audit("token_revoked", client_id=row["client_id"], kind=row["kind"], ip=get_remote_address(request))
    return Response(status_code=200)


# -------------------------------------------------------------------
# MCP proxy - validation + filtrage par scope
# -------------------------------------------------------------------
def _authenticate_bearer(auth_header: str) -> dict[str, Any]:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"'})
    token = auth_header.split(None, 1)[1].strip()
    with db() as con:
        row = con.execute(
            "SELECT * FROM tokens WHERE token_hash=? AND kind='access'", (_hash(token),)
        ).fetchone()
    if not row or row["revoked"] or row["expires_at"] < int(time.time()):
        raise HTTPException(401, {"error": "invalid_token"})
    return {"client_id": row["client_id"], "scope": row["scope"]}


def _allowed_tools(scope: str) -> set[str] | None:
    """Retourne le set autorise, ou None si tous autorises (admin)."""
    scopes = [s for s in scope.split() if s]
    if "admin" in scopes:
        return None
    allowed: set[str] = set()
    for s in scopes:
        allowed |= SCOPE_TOOLS.get(s, set())
    return allowed


async def _filter_tools_list(body: bytes, allowed: set[str] | None) -> bytes:
    if allowed is None:
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    result = data.get("result")
    if not isinstance(result, dict):
        return body
    tools = result.get("tools")
    if not isinstance(tools, list):
        return body
    result["tools"] = [t for t in tools if t.get("name") in allowed]
    return json.dumps(data).encode()


async def _check_tools_call(payload: dict[str, Any], allowed: set[str] | None) -> None:
    if allowed is None:
        return
    params = payload.get("params") or {}
    name = params.get("name")
    if name and name not in allowed:
        raise HTTPException(403, {"error": "insufficient_scope", "tool": name})


@app.api_route("/mcp-oauth/mcp", methods=["GET", "POST", "DELETE", "OPTIONS"])
@app.api_route("/mcp-oauth/mcp/", methods=["GET", "POST", "DELETE", "OPTIONS"])
@limiter.limit("60/minute")
async def mcp_proxy(request: Request) -> Response:
    principal = _authenticate_bearer(request.headers.get("authorization", ""))
    allowed = _allowed_tools(principal["scope"])

    body = await request.body()
    tool_call_name: str | None = None
    if request.method == "POST" and body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("method") == "tools/call":
                await _check_tools_call(payload, allowed)
                tool_call_name = (payload.get("params") or {}).get("name")
        except json.JSONDecodeError:
            pass

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "authorization", "content-length"}
    }
    forward_headers.setdefault("accept", "application/json, text/event-stream")
    forward_headers.setdefault("content-type", "application/json")

    async with httpx.AsyncClient(timeout=60.0) as client:
        upstream = await client.request(
            request.method,
            MCP_INTERNAL_URL,
            content=body if body else None,
            headers=forward_headers,
            params=dict(request.query_params),
        )

    response_body = upstream.content
    if request.method == "POST" and body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("method") == "tools/list":
                response_body = await _filter_tools_list(response_body, allowed)
        except json.JSONDecodeError:
            pass

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in {"content-length", "transfer-encoding", "connection"}
    }

    audit(
        "mcp_call",
        client_id=principal["client_id"],
        scope=principal["scope"].replace(" ", ","),
        status=upstream.status_code,
        tool=tool_call_name or "-",
        ip=get_remote_address(request),
    )

    return Response(content=response_body, status_code=upstream.status_code, headers=resp_headers)


# -------------------------------------------------------------------
# 401 avec resource_metadata pour MCP (RFC 9728)
# -------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exc_handler(_request: Request, exc: HTTPException) -> Response:
    headers = exc.headers or {}
    if exc.status_code == 401 and "www-authenticate" not in {k.lower() for k in headers}:
        headers = {
            **headers,
            "WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"',
        }
    body = exc.detail if isinstance(exc.detail, (dict, list)) else {"error": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code, headers=headers)
