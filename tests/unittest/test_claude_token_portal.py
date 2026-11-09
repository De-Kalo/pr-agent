import re
from urllib.parse import parse_qs, urlsplit

import httpx
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
