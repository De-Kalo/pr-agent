from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo.ai_handlers import claude_code
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.servers import github_app

BOT = "dbarnacle-bots-de-kalo[bot]"
API_URL = "https://api.github.com/repos/org/repo/pulls/1"
GOOD = '<!-- imparity-requested-by: {"githubId":42,"login":"alice","task":"t1"} -->'


def _settings(monkeypatch, bots=(BOT,)):
    monkeypatch.setattr(github_app, "global_settings", {
        "github_app.author_claude_tokens": True, "github_app.author_claude_model": "claude-sonnet-4-6",
        "github_app.requester_delegating_bots": list(bots),
    })
    monkeypatch.setenv("PR_AGENT_CLAUDE_TOKEN_7_42", "alice-token")
    monkeypatch.setenv("PR_AGENT_CLAUDE_TOKEN_7_99", "mallory-token")


def _pr_body(action="opened", *, pr_body=GOOD, author=(BOT, 500, "Bot"), sender=(BOT, 500, "Bot")):
    return {
        "action": action,
        "installation": {"id": 7},
        "sender": {"login": sender[0], "id": sender[1], "type": sender[2]},
        "pull_request": {
            "user": {"login": author[0], "id": author[1], "type": author[2]},
            "body": pr_body, "draft": False, "state": "open", "url": API_URL,
        },
    }


def _comment_body(commenter_id, *, pr_body=GOOD, author=(BOT, 500, "Bot")):
    return {
        "action": "created",
        "installation": {"id": 7},
        "sender": {"login": "c", "id": commenter_id, "type": "User"},
        "issue": {
            "user": {"login": author[0], "id": author[1], "type": author[2]},
            "body": pr_body, "pull_request": {"url": API_URL},
        },
        "comment": {"id": 1, "body": "/review"},
    }


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # automatic path
        (_pr_body(), (7, 42, True)),
        # only "opened": on later events a human may have edited the marker into the body before the bot acted
        (_pr_body("ready_for_review"), None),
        (_pr_body("edited"), None),
        (_pr_body("synchronize"), None),
        (_pr_body("reopened"), None),
        (_pr_body("ready_for_review", sender=("human", 99, "User")), None),
        (_pr_body("edited", sender=("human", 99, "User")), None),  # a human inserting someone else's marker
        (_pr_body(sender=(BOT, 500, "User")), None),  # bot's id but not a Bot sender
        (_pr_body(sender=(BOT, 501, "Bot")), None),  # bot's login but a different id
        # allowlisted login but not a Bot: the marker is ignored, only the author's own tokens apply
        (_pr_body(author=(BOT, 500, "User"), sender=(BOT, 500, "User")), (7, 500, False)),
        (_pr_body(sender=("human", 99, "User")), None),  # a human edit or toggle cannot redirect spending
        (_pr_body("opened", sender=("other[bot]", 501, "Bot")), None),  # a different bot
        (_pr_body(author=("human", 99, "User"), sender=("human", 99, "User")), (7, 99, False)),  # marker ignored
        (_pr_body(author=("human", 99, "User"), sender=("human", 99, "User"), pr_body=GOOD.replace("42", "98")),
         (7, 99, False)),
        (_pr_body(author=("other[bot]", 501, "Bot"), sender=("other[bot]", 501, "Bot")), None),  # not allowlisted
        # malformed markers
        (_pr_body(pr_body="<!-- imparity-requested-by: {not json} -->"), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: {"githubId":"42"} -->'), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: {"githubId":true} -->'), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: {"githubId":-1} -->'), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: {"githubId":0} -->'), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: {"githubId":42.0} -->'), None),
        # more than one marker: text a requester controls could precede the bot's own marker
        (_pr_body(pr_body=GOOD.replace("42", "99") + "\n" + GOOD), None),
        (_pr_body(pr_body=GOOD + "\n" + GOOD), None),
        (_pr_body(pr_body='<!-- imparity-requested-by: [42] -->'), None),
        (_pr_body(pr_body=None), None),
        (_pr_body(pr_body="no marker"), None),
        # comment path
        (_comment_body(42), (7, 42, True)),  # the requester spends their own tokens
        (_comment_body(99), None),  # anyone else gets the regular chain
        (_comment_body(42, author=("human", 99, "User")), None),  # marker on a human PR
        (_comment_body(42, author=("other[bot]", 501, "Bot")), None),
        (_comment_body(42, pr_body="<!-- imparity-requested-by: {oops} -->"), None),
        (_comment_body(42, pr_body=GOOD.replace("42", "99") + GOOD), None),
    ],
)
def test_identity(monkeypatch, body, expected):
    _settings(monkeypatch)
    assert github_app._pr_author_identity(body) == expected


def test_disabled_by_default(monkeypatch):
    _settings(monkeypatch, bots=())
    assert github_app._pr_author_identity(_pr_body()) is None
    assert github_app._pr_author_identity(_comment_body(42)) is None
    assert github_app._is_delegating_bot_pr_event(_pr_body(), "pull_request", "opened") is False


def test_allowlist_as_bare_string(monkeypatch):
    _settings(monkeypatch)
    github_app.global_settings["github_app.requester_delegating_bots"] = BOT.upper()
    assert github_app._pr_author_identity(_pr_body()) == (7, 42, True)


async def test_disabled_allowlist_still_drops_bot_event(monkeypatch):
    _settings(monkeypatch, bots=())
    factory, delegated_agent, default_agent = await _dispatch_opened(monkeypatch, _pr_body())
    factory.assert_called_once_with()  # only the default agent, built before the bot check
    delegated_agent.handle_request.assert_not_awaited()
    default_agent.handle_request.assert_not_awaited()


async def _run_comment(monkeypatch, body):
    monkeypatch.setattr(github_app, "get_git_provider_with_context", Mock())
    default_agent = Mock(handle_request=AsyncMock(return_value=True))
    delegated_agent = Mock(handle_request=AsyncMock(return_value=True))
    factory = Mock(return_value=delegated_agent)
    monkeypatch.setattr(github_app, "PRAgent", factory)
    await github_app.handle_comments_on_pr(
        body, "issue_comment", "c", body["sender"]["id"], "created", {}, default_agent
    )
    return factory, default_agent


async def test_requester_comment_uses_requester_token(monkeypatch):
    _settings(monkeypatch)
    factory, default_agent = await _run_comment(monkeypatch, _comment_body(42))
    assert factory.call_args.kwargs["ai_handler"].keywords["token"] == "alice-token"
    default_agent.handle_request.assert_not_awaited()


@pytest.mark.parametrize("commenter_id", [99, 500])
async def test_other_commenter_never_uses_requester_token(monkeypatch, commenter_id):
    _settings(monkeypatch)
    factory, default_agent = await _run_comment(monkeypatch, _comment_body(commenter_id))
    factory.assert_not_called()
    assert default_agent.handle_request.await_count == 1


async def test_marker_naming_member_without_token_falls_back(monkeypatch):
    _settings(monkeypatch)
    body = _comment_body(77, pr_body=GOOD.replace("42", "77"))
    factory, default_agent = await _run_comment(monkeypatch, body)
    factory.assert_not_called()
    assert default_agent.handle_request.await_count == 1


async def _dispatch_opened(monkeypatch, body, action="opened"):
    monkeypatch.setattr(github_app, "apply_repo_settings", Mock())
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _: True)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda _: ["/review"])
    monkeypatch.setattr(github_app, "get_git_provider_with_context", Mock())
    default_agent = Mock(handle_request=AsyncMock(return_value=True))
    delegated_agent = Mock(handle_request=AsyncMock(return_value=True))
    factory = Mock(side_effect=lambda **kwargs: delegated_agent if kwargs else default_agent)
    monkeypatch.setattr(github_app, "PRAgent", factory)
    settings = deepcopy(github_app.get_settings())
    settings.set("github_app.handle_pr_actions", ["opened", "ready_for_review"])
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", False)
    settings.set("github.ignore_bot_pr", True)
    with request_cycle_context({"settings": settings}):
        await github_app._dispatch_request(body, "pull_request", action)
    return factory, delegated_agent, default_agent


async def test_bot_opened_pr_reviewed_on_requester_token(monkeypatch):
    _settings(monkeypatch)
    factory, delegated_agent, default_agent = await _dispatch_opened(monkeypatch, _pr_body())
    assert factory.call_args.kwargs["ai_handler"].keywords["token"] == "alice-token"
    assert delegated_agent.handle_request.await_count == 1
    default_agent.handle_request.assert_not_awaited()


async def test_bot_readied_pr_is_not_reviewed_on_requester_token(monkeypatch):
    _settings(monkeypatch)
    body = _pr_body("ready_for_review")
    factory, delegated_agent, default_agent = await _dispatch_opened(monkeypatch, body, "ready_for_review")
    factory.assert_called_once_with()  # still dropped as a bot event; only the default agent was built
    delegated_agent.handle_request.assert_not_awaited()
    default_agent.handle_request.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        _pr_body(sender=("human", 99, "User")),
        _pr_body(pr_body="<!-- imparity-requested-by: {bad} -->"),
        _pr_body(author=("human", 99, "User"), sender=("human", 99, "User")),
        _pr_body(pr_body=GOOD.replace("42", "99") + GOOD),
    ],
)
async def test_spoofed_open_does_not_use_requester_token(monkeypatch, body):
    _settings(monkeypatch)
    factory, delegated_agent, _ = await _dispatch_opened(monkeypatch, body)
    assert all(call.kwargs.get("ai_handler") is None or
               call.kwargs["ai_handler"].keywords["token"] != "alice-token" for call in factory.call_args_list)


async def test_footer_names_the_requester(monkeypatch):
    from pr_agent.tools.pr_reviewer import _review_model_footer

    completion = AsyncMock(return_value=("review", "stop"))
    monkeypatch.setattr(claude_code, "_claude_code_completion", completion)
    settings = deepcopy(github_app.get_settings())
    with request_cycle_context({"settings": settings}):
        init_run_details()
        handler = claude_code.ClaudeCodeAIHandler(token="t", credential_owner="requester's ")
        await handler.chat_completion("anthropic/claude-sonnet-4-6", "s", "u")
        details = get_run_details()
        details.model_used = "anthropic/claude-sonnet-4-6"
        assert "(requester's Claude Code setup token)" in _review_model_footer()
