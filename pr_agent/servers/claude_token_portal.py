import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from html import escape
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pr_agent.config_loader import global_settings
from pr_agent.log import get_logger

router = APIRouter()
_PATH = "/claude-token"
_CALLBACK = f"{_PATH}/callback"
_STATE_COOKIE = "__Host-pr-agent-claude-state"
_SESSION_COOKIE = "__Host-pr-agent-claude-session"
_SESSION_SECONDS = 600
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(frozen=True)
class _PortalConfig:
    org: str
    installation_id: int
    callback_url: str
    client_id: str
    client_secret: str = field(repr=False)
    session_secret: str = field(repr=False)


def _portal_config() -> _PortalConfig:
    settings = global_settings
    org = settings.get("github_app.claude_token_org")
    if not settings.get("github_app.author_claude_tokens", False) or not org:
        raise HTTPException(404)
    installation_id = settings.get("github_app.claude_token_installation_id")
    callback_url = settings.get("github_app.claude_token_callback_url") or ""
    client_id = settings.get("github_app.claude_token_client_id")
    client_secret = settings.get("github_app.claude_token_client_secret")
    session_secret = settings.get("github_app.claude_token_session_secret") or ""
    url = urlsplit(callback_url)
    if (
        not re.fullmatch(r"[A-Za-z0-9-]+", org)
        or type(installation_id) is not int or installation_id <= 0
        or url.scheme != "https" or not url.hostname or url.path != _CALLBACK
        or url.username or url.password or url.query or url.fragment
        or not client_id or not client_secret or len(session_secret.encode()) < 32
    ):
        raise HTTPException(503, "Token registration is unavailable.")
    return _PortalConfig(org, installation_id, callback_url, client_id, client_secret, session_secret)


def _token_store():
    from pr_agent.secret_providers.aws_secrets_manager_provider import AWSSecretsManagerProvider

    return AWSSecretsManagerProvider(settings=global_settings, require_config_secret=False)


def _secret_name(installation_id: int, user_id: int) -> str:
    return f"pr-agent/claude-code/{installation_id}/{user_id}"


def get_registered_token(installation_id: int, user_id: int) -> str:
    raw = _token_store().get_secret(_secret_name(installation_id, user_id))
    token = json.loads(raw).get("token", "") if raw else ""
    return token if isinstance(token, str) else ""


def get_registered_chatgpt_plan_token(installation_id: int, user_id: int) -> str:
    raw = _token_store().get_secret(f"pr-agent/chatgpt-plan/{installation_id}/{user_id}")
    if not raw:
        return ""
    credentials = json.loads(raw)
    expires_at = credentials.get("expires_at")
    token = credentials.get("access_token")
    if type(expires_at) is int and expires_at > time.time() + 30 and isinstance(token, str):
        return token.strip()
    return ""


def _save_registered_token(installation_id: int, user_id: int, token: str) -> None:
    _token_store().store_secret(_secret_name(installation_id, user_id), json.dumps({"token": token}))


def _audience(config: _PortalConfig, cookie: str) -> str:
    return f"{cookie}:{config.org.lower()}:{config.installation_id}"


def _set_cookie(response: RedirectResponse, config: _PortalConfig, cookie: str, **claims) -> None:
    value = jwt.encode(
        {**claims, "aud": _audience(config, cookie), "exp": int(time.time()) + _SESSION_SECONDS},
        config.session_secret,
        algorithm="HS256",
    )
    response.set_cookie(
        cookie, value, max_age=_SESSION_SECONDS, secure=True, httponly=True, samesite="lax"
    )


def _read_cookie(request: Request, config: _PortalConfig, cookie: str) -> dict:
    try:
        return jwt.decode(
            request.cookies.get(cookie, ""),
            config.session_secret,
            algorithms=["HS256"],
            audience=_audience(config, cookie),
            options={"require": ["exp", "aud"]},
        )
    except jwt.InvalidTokenError:
        return {}


@router.get(_PATH, response_class=HTMLResponse)
async def token_page(request: Request):
    config = _portal_config()
    session = _read_cookie(request, config, _SESSION_COOKIE)
    if session:
        csrf = escape(session["csrf"], quote=True)
        content = f"""
<p>Run <code>claude setup-token</code> in your terminal, then paste your token below.</p>
<form method="post" action="{_PATH}" autocomplete="off">
  <input type="hidden" name="csrf" value="{csrf}">
  <label for="token">Claude Code setup token</label>
  <input type="password" name="token" id="token" maxlength="4096" autocomplete="new-password" required>
  <button name="action" value="save">Save or replace token</button>
  <button name="action" value="remove" formnovalidate>Remove token</button>
</form>"""
    else:
        content = (
            '<p>Sign in with your GitHub organization account.</p>'
            f'<a href="{_PATH}/login">Sign in with GitHub</a>'
        )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR-Agent Claude setup</title><style>
body {{ font: 1rem system-ui, sans-serif; margin: 3rem auto; padding: 0 1rem; max-width: 36rem; line-height: 1.6; }}
label {{ display: block; }} input[type=password] {{ display: block; width: 100%; box-sizing: border-box;
font: inherit; padding: .5rem; margin-bottom: 1rem; }} button {{ font: inherit; margin: 0 .5rem .5rem 0; }}
</style></head><body><main><h1>Claude Code for PR reviews</h1>{content}</main></body></html>""",
        headers=_HEADERS,
    )


@router.get(f"{_PATH}/login")
async def token_login():
    config = _portal_config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({
        "client_id": config.client_id,
        "redirect_uri": config.callback_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
    })
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", headers=_HEADERS)
    _set_cookie(response, config, _STATE_COOKIE, state=state, verifier=verifier)
    return response


async def _github_member_id(config: _PortalConfig, code: str, verifier: str) -> int:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "redirect_uri": config.callback_url,
                "code_verifier": verifier,
            },
        )
        response.raise_for_status()
        access_token = response.json().get("access_token")
        if not access_token:
            raise HTTPException(403, "GitHub sign-in was denied.")
        response = await client.get(
            f"https://api.github.com/user/memberships/orgs/{config.org}",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {access_token}"},
        )
        if response.status_code in (403, 404):
            raise HTTPException(403, "Active organization membership is required.")
        response.raise_for_status()
        membership = response.json()
    user = membership.get("user") or {}
    user_id = user.get("id")
    if membership.get("state") != "active" or user.get("type") != "User" or type(user_id) is not int or user_id <= 0:
        raise HTTPException(403, "Active organization membership is required.")
    return user_id


@router.get(_CALLBACK)
async def token_callback(request: Request):
    config = _portal_config()
    state_cookie = _read_cookie(request, config, _STATE_COOKIE)
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not state_cookie or not code or not secrets.compare_digest(state.encode(), state_cookie["state"].encode()):
        raise HTTPException(403, "Invalid or expired GitHub sign-in.")
    try:
        user_id = await _github_member_id(config, code, state_cookie["verifier"])
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "GitHub sign-in is temporarily unavailable.") from None
    response = RedirectResponse(_PATH, status_code=303, headers=_HEADERS)
    response.delete_cookie(_STATE_COOKIE, secure=True, httponly=True, samesite="lax")
    _set_cookie(response, config, _SESSION_COOKIE, user_id=user_id, csrf=secrets.token_urlsafe(32))
    return response


@router.post(_PATH, response_class=HTMLResponse)
async def token_save(request: Request):
    config = _portal_config()
    session = _read_cookie(request, config, _SESSION_COOKIE)
    if not session:
        raise HTTPException(403, "Sign in with GitHub first.")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Unsupported form type.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16_384:
            raise HTTPException(413, "Form is too large.")
        body.extend(chunk)
    try:
        form = parse_qs(body.decode(), max_num_fields=8)
    except ValueError:
        raise HTTPException(400, "Invalid form.") from None
    csrf = form.get("csrf", [""])[0]
    if not secrets.compare_digest(csrf.encode(), session["csrf"].encode()):
        raise HTTPException(403, "Invalid or expired form.")
    action = form.get("action", [""])[0]
    token = form.get("token", [""])[0].strip() if action == "save" else ""
    if action not in ("save", "remove") or (action == "save" and not re.fullmatch(r"[!-~]{20,4096}", token)):
        raise HTTPException(400, "Enter a valid Claude Code setup token.")
    try:
        await asyncio.to_thread(_save_registered_token, config.installation_id, session["user_id"], token)
    except Exception:
        get_logger().warning("Could not update a registered Claude Code token.")
        raise HTTPException(503, "Token storage is temporarily unavailable.") from None
    status = "Token saved." if token else "Token removed."
    return HTMLResponse(f'<p>{status}</p><a href="{_PATH}">Back to setup</a>', headers=_HEADERS)
