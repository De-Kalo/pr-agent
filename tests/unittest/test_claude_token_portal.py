import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from pr_agent.servers import claude_token_portal as portal
from pr_agent.servers import github_app


@pytest.fixture
def portal_settings(monkeypatch):
    settings = {
        "github_app.author_claude_tokens": True,
        "github_app.claude_token_org": "internal-org",
        "github_app.claude_token_installation_id": 7,
        "github_app.claude_token_callback_url": "https://example.test/claude-token/callback",
        "github_app.claude_token_client_id": "client",
        "github_app.claude_token_client_secret": "secret",
        "github_app.claude_token_session_secret": "test-session-key-that-is-at-least-32-bytes",
    }
    monkeypatch.setattr(portal, "global_settings", settings)
    monkeypatch.setattr(github_app, "global_settings", settings)
    return settings


def test_portal_binds_token_to_signed_in_user(portal_settings, monkeypatch):
    records = {}

    class Store:
        def store_secret(self, name, value):
            records[name] = value

        def get_secret(self, name):
            return records.get(name, "")

    async def github_member(config, code, verifier):
        assert config.org == "internal-org"
        assert code == "github-code"
        assert verifier
        return 42

    monkeypatch.setattr(portal, "_github_member_id", github_member)
    monkeypatch.setattr(portal, "_token_store", Store)
    client = TestClient(github_app.app, base_url="https://example.test")
    token = "test-claude-token-for-user-42"

    assert client.post("/claude-token", data={"token": token}).status_code == 403
    login = client.get("/claude-token/login", follow_redirects=False)
    assert login.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "Secure" in login.headers["set-cookie"]
    query = parse_qs(urlsplit(login.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    callback = "/claude-token/callback"
    assert client.get(callback, params={"code": "github-code", "state": "forged"}).status_code == 403

    result = client.get(callback, params={"code": "github-code", "state": query["state"][0]})
    csrf = re.search(r'name="csrf" value="([^"]+)"', result.text).group(1)
    assert result.headers["cache-control"] == "no-store"
    assert client.post("/claude-token", data={"action": "save", "csrf": "forged", "token": token}).status_code == 403
    assert records == {}

    saved = client.post("/claude-token", data={"action": "save", "csrf": csrf, "token": token, "user_id": "99"})
    assert saved.status_code == 200
    assert token not in saved.text
    assert token not in client.get("/claude-token").text
    assert portal.get_registered_token(7, 42) == token
    assert portal.get_registered_token(7, 99) == ""

    removed = client.post("/claude-token", data={"action": "remove", "csrf": csrf})
    assert removed.status_code == 200
    assert portal.get_registered_token(7, 42) == ""


@pytest.mark.parametrize("state", ["active", "pending"])
async def test_portal_checks_org_membership(portal_settings, monkeypatch, state):
    def handle(request):
        if request.url.host == "github.com":
            assert "code_verifier=verifier" in request.content.decode()
            return httpx.Response(200, json={"access_token": "github-access-token"})
        assert request.url.path == "/user/memberships/orgs/internal-org"
        assert request.headers["Authorization"] == "Bearer github-access-token"
        return httpx.Response(200, json={"state": state, "user": {"id": 42, "type": "User"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(portal.httpx, "AsyncClient", lambda **_: client)
    if state == "active":
        assert await portal._github_member_id(portal._portal_config(), "code", "verifier") == 42
    else:
        with pytest.raises(HTTPException) as error:
            await portal._github_member_id(portal._portal_config(), "code", "verifier")
        assert error.value.status_code == 403


async def test_webhook_loads_only_the_portal_installations_token(portal_settings, monkeypatch):
    monkeypatch.setattr(portal, "get_registered_token", lambda installation, user: f"token-{installation}-{user}")
    body = {
        "installation": {"id": 7},
        "sender": {"id": 42},
        "pull_request": {"user": {"id": 42, "type": "User"}},
    }
    assert await github_app._pr_author_claude_token(body) == "token-7-42"
    body["installation"]["id"] = 8
    assert await github_app._pr_author_claude_token(body) == ""


def _access_token(exp, client_id="codex-client"):
    return jwt.encode({"exp": exp, "client_id": client_id}, "unused-signing-key-of-32-bytes-min", algorithm="HS256")


def _plan_record(exp, refresh_token="refresh-1"):
    return json.dumps({
        "access_token": _access_token(exp),
        "refresh_token": refresh_token,
        "account_id": "account-1",
        "client_id": "codex-client",
        "expires_at": exp,
    })


class VersionedStore:
    """In-memory stand-in for the Secrets Manager versioning used by the token portal."""

    def __init__(self, records=None):
        self.records = {name: (value, 1) for name, value in (records or {}).items()}

    def get_secret(self, name):
        return self.get_secret_version(name)[0]

    def get_secret_version(self, name):
        value, version = self.records.get(name, ("", 0))
        return value, str(version) if value else ""

    def store_secret(self, name, value):
        self.records[name] = (value, self.records.get(name, ("", 0))[1] + 1)

    def replace_secret_if_current(self, name, value, version_id):
        if self.get_secret_version(name)[1] != version_id:
            return False
        self.store_secret(name, value)
        return True


def _refresh_response(url, access_exp=90_000, refresh_token="refresh-2"):
    return httpx.Response(200, json={"access_token": _access_token(access_exp), "refresh_token": refresh_token},
                          request=httpx.Request("POST", url))


async def test_webhook_loads_only_a_current_plan_token_for_the_pr_author(portal_settings, monkeypatch):
    name = "pr-agent/chatgpt-plan/7/42"
    store = VersionedStore({name: _plan_record(100_000)})
    monkeypatch.setattr(portal, "_token_store", lambda: store)
    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    body = {
        "installation": {"id": 7},
        "sender": {"id": 42},
        "pull_request": {"user": {"id": 42, "type": "User"}},
    }
    plan = await github_app._pr_author_chatgpt_plan_credentials(body)
    assert (plan.access_token, plan.account_id) == (_access_token(100_000), "account-1")
    assert "access_token" not in repr(plan)
    store.store_secret(name, "{}")
    assert await github_app._pr_author_chatgpt_plan_credentials(body) is None
    store.store_secret(name, _plan_record(100_000))
    body["sender"]["id"] = 43
    assert await github_app._pr_author_chatgpt_plan_credentials(body) is None


def test_plan_token_refreshes_before_expiry(monkeypatch):
    name = "pr-agent/chatgpt-plan/7/42"
    store = VersionedStore({name: _plan_record(2000)})

    def refresh(url, json, **kwargs):
        assert url == "https://auth.openai.com/oauth/token"
        assert json["grant_type"] == "refresh_token"
        assert (json["client_id"], json["refresh_token"]) == ("codex-client", "refresh-1")
        return _refresh_response(url)

    monkeypatch.setattr(portal, "_token_store", lambda: store)
    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    monkeypatch.setattr(portal.httpx, "post", refresh)
    plan = portal.get_registered_chatgpt_plan_credentials(7, 42)
    assert plan.access_token == _access_token(90_000)
    stored = json.loads(store.get_secret(name))
    assert (stored["refresh_token"], stored["expires_at"]) == ("refresh-2", 90_000)


@pytest.mark.parametrize("change", ["disconnected", "replaced", "refreshed by another worker"])
def test_plan_refresh_does_not_overwrite_a_change_made_during_refresh(monkeypatch, change):
    name = "pr-agent/chatgpt-plan/7/42"
    store = VersionedStore({name: _plan_record(2000)})
    newer = {"disconnected": "{}", "replaced": _plan_record(80_000, "refresh-new"),
             "refreshed by another worker": _plan_record(85_000, "refresh-other")}[change]

    def refresh(url, **kwargs):
        store.store_secret(name, newer)
        return _refresh_response(url)

    monkeypatch.setattr(portal, "_token_store", lambda: store)
    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    monkeypatch.setattr(portal.httpx, "post", refresh)
    plan = portal.get_registered_chatgpt_plan_credentials(7, 42)
    assert store.get_secret(name) == newer
    if change == "disconnected":
        assert plan is None
    else:
        assert plan.access_token == json.loads(newer)["access_token"]


@pytest.mark.parametrize("other_worker_refreshed", [True, False])
def test_failed_plan_refresh_uses_another_workers_record(monkeypatch, other_worker_refreshed):
    name = "pr-agent/chatgpt-plan/7/42"
    store = VersionedStore({name: _plan_record(1010)})

    def refresh(url, **kwargs):
        if other_worker_refreshed:
            store.store_secret(name, _plan_record(90_000, "refresh-2"))
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(portal, "_token_store", lambda: store)
    monkeypatch.setattr(portal.time, "time", lambda: 1000)
    monkeypatch.setattr(portal.httpx, "post", refresh)
    plan = portal.get_registered_chatgpt_plan_credentials(7, 42)
    assert (plan is not None) == other_worker_refreshed


def test_portal_connects_chatgpt_from_codex_auth(portal_settings, monkeypatch):
    portal_settings["github_app.author_chatgpt_plan_model"] = "gpt-5.6-sol"
    store = VersionedStore()

    async def github_member(config, code, verifier):
        return 42

    monkeypatch.setattr(portal, "_github_member_id", github_member)
    monkeypatch.setattr(portal, "_token_store", lambda: store)
    client = TestClient(github_app.app, base_url="https://example.test")
    login = client.get("/claude-token/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    page = client.get("/claude-token/callback", params={"code": "github-code", "state": state})
    assert "Connect ChatGPT" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    access_token = _access_token(4_000_000_000)
    auth = json.dumps({"auth_mode": "chatgpt", "tokens": {
        "id_token": "id", "access_token": access_token, "refresh_token": "refresh-1", "account_id": "account-1",
    }})

    bad = client.post("/claude-token", data={"action": "save_chatgpt", "csrf": csrf, "codex_auth": "{}"})
    assert bad.status_code == 400
    saved = client.post("/claude-token", data={"action": "save_chatgpt", "csrf": csrf, "codex_auth": auth})
    assert saved.status_code == 200
    assert access_token not in saved.text
    plan = portal.get_registered_chatgpt_plan_credentials(7, 42)
    assert (plan.access_token, plan.account_id) == (access_token, "account-1")

    removed = client.post("/claude-token", data={"action": "remove_chatgpt", "csrf": csrf})
    assert removed.status_code == 200
    assert portal.get_registered_chatgpt_plan_credentials(7, 42) is None


def test_portal_hides_chatgpt_until_a_plan_model_is_configured(portal_settings, monkeypatch):
    async def github_member(config, code, verifier):
        return 42

    monkeypatch.setattr(portal, "_github_member_id", github_member)
    client = TestClient(github_app.app, base_url="https://example.test")
    login = client.get("/claude-token/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    page = client.get("/claude-token/callback", params={"code": "github-code", "state": state})
    assert "Connect ChatGPT" not in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/claude-token", data={"action": "remove_chatgpt", "csrf": csrf}).status_code == 404
