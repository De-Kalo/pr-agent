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
_FORM_LIMIT = 65_536
_CHATGPT_REFRESH_URL = "https://auth.openai.com/oauth/token"
_CHATGPT_REFRESH_MARGIN_SECONDS = 3600
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


@dataclass(frozen=True)
class ChatGPTPlanCredentials:
    access_token: str = field(repr=False)
    account_id: str


def _chatgpt_secret_name(installation_id: int, user_id: int) -> str:
    return f"pr-agent/chatgpt-plan/{installation_id}/{user_id}"


def _chatgpt_record(access_token: str, refresh_token: str, account_id: str) -> dict | None:
    """Build the stored record; expiry and OAuth client come from the access token's own claims."""
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    expires_at = claims.get("exp")
    client_id = claims.get("client_id")
    if type(expires_at) is not int or not isinstance(client_id, str) or not client_id:
        return None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "client_id": client_id,
        "expires_at": expires_at,
    }


def _parse_codex_auth(raw: str) -> dict | None:
    """Accept the auth.json written by `codex login` with a ChatGPT account."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    values = [tokens.get(key) for key in ("access_token", "refresh_token", "account_id")]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return _chatgpt_record(*(value.strip() for value in values))


def _load_chatgpt_record(store, name: str) -> tuple[dict | None, str]:
    raw, version_id = store.get_secret_version(name)
    record = json.loads(raw) if raw else {}
    if (
        not isinstance(record, dict)
        or not all(isinstance(record.get(key), str) and record[key]
                   for key in ("access_token", "refresh_token", "account_id", "client_id"))
        or type(record.get("expires_at")) is not int
    ):
        return None, version_id
    return record, version_id


def _refresh_chatgpt_record(record: dict) -> dict:
    response = httpx.post(
        _CHATGPT_REFRESH_URL,
        json={
            "client_id": record["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
            "scope": "openid profile email",
        },
        timeout=10,
        trust_env=False,
    )
    response.raise_for_status()
    data = response.json()
    refresh_token = data.get("refresh_token") or record["refresh_token"]
    access_token = data.get("access_token")
    refreshed = _chatgpt_record(access_token, refresh_token, record["account_id"]) if access_token else None
    if refreshed is None:
        raise ValueError("The ChatGPT token refresh returned no usable access token.")
    return refreshed


def get_registered_chatgpt_plan_credentials(installation_id: int, user_id: int) -> ChatGPTPlanCredentials | None:
    store = _token_store()
    name = _chatgpt_secret_name(installation_id, user_id)
    record, version_id = _load_chatgpt_record(store, name)
    if record is None:
        return None
    if record["expires_at"] <= time.time() + _CHATGPT_REFRESH_MARGIN_SECONDS:
        try:
            refreshed = _refresh_chatgpt_record(record)
        except (httpx.HTTPError, ValueError):
            get_logger().warning("Could not refresh a registered ChatGPT plan token.")
            refreshed = None
        # The conditional replace fails if the member disconnected or reconnected, or another worker
        # refreshed first; refresh tokens rotate, so then use whatever record is current.
        if refreshed is not None and store.replace_secret_if_current(name, json.dumps(refreshed), version_id):
            record = refreshed
        else:
            record, _ = _load_chatgpt_record(store, name)
            if record is None or record["expires_at"] <= time.time() + 30:
                return None
    return ChatGPTPlanCredentials(record["access_token"], record["account_id"])


def _save_registered_token(installation_id: int, user_id: int, token: str) -> None:
    _token_store().store_secret(_secret_name(installation_id, user_id), json.dumps({"token": token}))


def _save_chatgpt_record(installation_id: int, user_id: int, record: dict) -> None:
    _token_store().store_secret(_chatgpt_secret_name(installation_id, user_id), json.dumps(record))


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


def _member_setup(installation_id: int, user_id: int) -> tuple[bool, bool]:
    """Return whether the member has a ChatGPT plan login and a Claude token, without refreshing anything."""
    chatgpt_record, _ = _load_chatgpt_record(_token_store(), _chatgpt_secret_name(installation_id, user_id))
    return chatgpt_record is not None, bool(get_registered_token(installation_id, user_id))


def _review_order(setup: tuple[bool, bool] | None, signed_in: bool) -> str:
    """Describe the order in which the author's reviews try each model source."""

    def status(ready: bool | None, ready_text: str, missing_text: str) -> str:
        if not signed_in:
            return ""
        if ready is None:
            return '<br><span class="status">Status unavailable right now.</span>'
        if ready:
            return f'<br><span class="status ok">✓ {ready_text}</span>'
        return f'<br><span class="status off">✗ {missing_text}. Skipped for your PRs.</span>'

    chatgpt_ready, claude_ready = setup if setup else (None, None)
    plan_model = global_settings.get("github_app.author_chatgpt_plan_model")
    claude_model = global_settings.get("github_app.author_claude_model") or "Claude"
    api_models = [global_settings.get("config.model"), *(global_settings.get("config.fallback_models") or [])]
    api_models = ", then ".join(f"<code>{escape(str(model))}</code>" for model in api_models if model)
    steps = []
    if plan_model:
        steps.append(
            f"<li><strong>ChatGPT plan</strong>: <code>{escape(plan_model)}</code> on your ChatGPT plan's Codex usage."
            + status(chatgpt_ready, "ChatGPT connected", "ChatGPT not connected") + "</li>"
        )
    steps.append(
        f"<li><strong>Claude Code</strong>: <code>{escape(claude_model)}</code> on your Claude subscription."
        + status(claude_ready, "Claude token saved", "No Claude token") + "</li>"
    )
    steps.append(
        f"<li><strong>OpenAI API</strong>: {api_models or 'the configured models'} on the team's API key."
        + ('<br><span class="status ok">✓ Always available</span>' if signed_in else "") + "</li>"
    )
    return f"""
<section class="order"><h2>How your PR reviews run</h2>
<p>When you open a PR, or comment a command such as <code>/review</code> on your own PR, PR-Agent tries
these in order and uses the first one that works:</p>
<ol>{"".join(steps)}</ol>
<p>A step is skipped when it isn't set up, has reached its usage limit, or fails. The <strong>Reviewed with</strong>
line at the bottom of each review shows which one ran. Commands that someone else comments on your PR, and
reviews after new pushes, use the OpenAI API.</p></section>"""


@router.get(_PATH, response_class=HTMLResponse)
async def token_page(request: Request):
    config = _portal_config()
    session = _read_cookie(request, config, _SESSION_COOKIE)
    if session:
        csrf = escape(session["csrf"], quote=True)
        try:
            setup = await asyncio.to_thread(_member_setup, config.installation_id, session["user_id"])
        except Exception:
            get_logger().warning("Could not read a member's review setup.")
            setup = None
        claude_section = f"""
<h2>Claude Code</h2>
<p>Run <code>claude setup-token</code> in your terminal, then paste your token below.</p>
<form method="post" action="{_PATH}" autocomplete="off">
  <input type="hidden" name="csrf" value="{csrf}">
  <label for="token">Claude Code setup token</label>
  <input type="password" name="token" id="token" maxlength="4096" autocomplete="new-password" required>
  <button name="action" value="save">Save or replace token</button>
  <button name="action" value="remove" formnovalidate>Remove token</button>
</form>"""
        chatgpt_section = ""
        if global_settings.get("github_app.author_chatgpt_plan_model"):
            chatgpt_section = f"""
<h2>ChatGPT plan</h2>
<p>Reviews of your PRs can use your ChatGPT plan's Codex usage first. Sign in with a separate Codex profile,
so your own Codex CLI stays signed in:</p>
<pre><code>mkdir -p ~/.codex-pr-agent
CODEX_HOME=~/.codex-pr-agent codex login</code></pre>
<p>Then paste the contents of <code>~/.codex-pr-agent/auth.json</code> below.</p>
<form method="post" action="{_PATH}" autocomplete="off">
  <input type="hidden" name="csrf" value="{csrf}">
  <label for="codex_auth">Codex auth.json</label>
  <textarea name="codex_auth" id="codex_auth" rows="6" maxlength="32768" spellcheck="false" required></textarea>
  <button name="action" value="save_chatgpt">Connect ChatGPT</button>
  <button name="action" value="remove_chatgpt" formnovalidate>Disconnect ChatGPT</button>
</form>"""
        content = _review_order(setup, signed_in=True) + chatgpt_section + claude_section
    else:
        content = _review_order(None, signed_in=False) + (
            '<p>Sign in with your GitHub organization account to see and change your setup.</p>'
            f'<a href="{_PATH}/login">Sign in with GitHub</a>'
        )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR-Agent review setup</title><style>
body {{ font: 1rem system-ui, sans-serif; margin: 3rem auto; padding: 0 1rem; max-width: 36rem; line-height: 1.6; }}
label {{ display: block; }} input[type=password], textarea {{ display: block; width: 100%; box-sizing: border-box;
font: inherit; padding: .5rem; margin-bottom: 1rem; }} button {{ font: inherit; margin: 0 .5rem .5rem 0; }}
.order {{ border: 1px solid #8884; border-radius: .5rem; padding: 0 1rem; margin-bottom: 2rem; }}
.order li {{ margin-bottom: .5rem; }} .status {{ font-size: .9rem; }}
.ok {{ color: #1a7f37; }} .off {{ color: #9a6700; }}
</style></head><body><main><h1>AI reviews for your PRs</h1>{content}</main></body></html>""",
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
        if len(body) + len(chunk) > _FORM_LIMIT:
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
    if action in ("save_chatgpt", "remove_chatgpt"):
        return await _update_chatgpt_plan(config, session["user_id"], action, form.get("codex_auth", [""])[0])
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


async def _update_chatgpt_plan(config: _PortalConfig, user_id: int, action: str, codex_auth: str) -> HTMLResponse:
    if not global_settings.get("github_app.author_chatgpt_plan_model"):
        raise HTTPException(404)
    record = {}
    if action == "save_chatgpt":
        record = _parse_codex_auth(codex_auth) if len(codex_auth) <= 32_768 else None
        if record is None:
            raise HTTPException(400, "Paste the auth.json written by `codex login` with a ChatGPT account.")
    try:
        await asyncio.to_thread(_save_chatgpt_record, config.installation_id, user_id, record)
    except Exception:
        get_logger().warning("Could not update a registered ChatGPT plan token.")
        raise HTTPException(503, "Token storage is temporarily unavailable.") from None
    status = "ChatGPT connected." if record else "ChatGPT disconnected."
    return HTMLResponse(f'<p>{status}</p><a href="{_PATH}">Back to setup</a>', headers=_HEADERS)
