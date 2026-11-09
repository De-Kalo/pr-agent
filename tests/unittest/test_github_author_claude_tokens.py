from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo.ai_handlers import claude_code
from pr_agent.algo.pr_processing import prefer_model, retry_with_fallback_models
from pr_agent.servers import github_app


@pytest.mark.parametrize(
    ("installation_id", "author_id", "sender_id", "enabled", "expected_token"),
    [
        (7, 42, 42, True, "author-token"),
        (7, 43, 43, True, "other-token"),
        (8, 42, 42, True, None),
        (7, 42, 43, True, None),
        (7, 42, 42, False, None),
    ],
)
async def test_opened_pr_uses_only_its_authors_token(
    monkeypatch, installation_id, author_id, sender_id, enabled, expected_token
):
    api_url = "https://api.github.com/repos/org/repo/pulls/1"
    body = {
        "action": "opened",
        "installation": {"id": installation_id},
        "sender": {"id": sender_id, "type": "User", "login": "sender"},
        "pull_request": {"user": {"id": author_id, "type": "User"}, "draft": False, "state": "open", "url": api_url},
    }
    monkeypatch.setenv("PR_AGENT_CLAUDE_TOKEN_7_42", "author-token")
    monkeypatch.setenv("PR_AGENT_CLAUDE_TOKEN_7_43", "other-token")
    monkeypatch.setattr(github_app, "global_settings", {"github_app.author_claude_tokens": enabled})
    monkeypatch.setattr(github_app, "apply_repo_settings", Mock())
    monkeypatch.setattr(github_app, "should_process_pr_logic", lambda _: True)
    monkeypatch.setattr(github_app, "get_pr_commands", lambda _: ["/review"])
    monkeypatch.setattr(github_app, "get_git_provider_with_context", Mock())
    default_agent = Mock(handle_request=AsyncMock(return_value=True))
    claude_agent = Mock(handle_request=AsyncMock(return_value=True))
    factory = Mock(side_effect=lambda **kwargs: claude_agent if kwargs else default_agent)
    monkeypatch.setattr(github_app, "PRAgent", factory)
    completion = AsyncMock(return_value=("review", "stop"))
    monkeypatch.setattr(claude_code, "_claude_code_completion", completion)

    settings = deepcopy(github_app.get_settings())
    settings.set("github_app.handle_pr_actions", ["opened"])
    settings.set("config.disable_auto_feedback", False)
    settings.set("github.publish_as_check_run", False)
    with request_cycle_context({"settings": settings}):
        result = await github_app._dispatch_request(body, "pull_request", "opened")
        if expected_token:
            factory_args = factory.call_args.kwargs["ai_handler"].keywords
            assert factory_args["token"] == expected_token
            handler = factory.call_args.kwargs["ai_handler"]()
            await handler.chat_completion("anthropic/claude-sonnet-4-6", "system", "diff")

    assert result is True
    assert claude_agent.handle_request.await_count == bool(expected_token)
    if expected_token:
        assert completion.await_args.kwargs["token"] == expected_token
        assert completion.await_args.args == ("claude-sonnet-4-6", "system", "diff")
    else:
        completion.assert_not_awaited()
    assert default_agent.handle_request.await_count == (0 if expected_token else 1)


@pytest.mark.parametrize("token", ["", "expired-token"])
async def test_claude_failure_uses_regular_openai_fallback(monkeypatch, token):
    settings = deepcopy(github_app.get_settings())
    settings.set("config.model", "gpt-5.6")
    settings.set("config.fallback_models", [])
    openai = AsyncMock(return_value=("review", "stop"))
    claude = AsyncMock(side_effect=RuntimeError("Token expired"))
    monkeypatch.setattr(claude_code.LiteLLMAIHandler, "chat_completion", openai)
    monkeypatch.setattr(claude_code, "_claude_code_completion", claude)
    with request_cycle_context({"settings": settings}):
        handler = claude_code.ClaudeCodeAIHandler(token=token)

        async def predict(model):
            return await handler.chat_completion(model, "system", "diff")

        with prefer_model("claude-sonnet-4-6" if token else None):
            assert await retry_with_fallback_models(predict) == ("review", "stop")
    assert claude.await_count == bool(token)
    openai.assert_awaited_once_with("gpt-5.6", "system", "diff", 0.2, None)


async def test_claude_cli_receives_only_the_selected_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "shared-token")
    monkeypatch.setenv("PR_AGENT_CLAUDE_TOKEN_7_43", "other-token")
    process = Mock(returncode=0)
    process.communicate = AsyncMock(return_value=(
        b'{"subtype":"success","is_error":false,"result":"review","stop_reason":"end_turn"}', None
    ))

    async def launch(*args, cwd, env, **kwargs):
        assert args[0] == "claude"
        assert "--safe-mode" in args
        assert args[args.index("--tools") + 1] == ""
        assert Path(args[args.index("--system-prompt-file") + 1]).read_text() == "system"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "author-token"
        assert "ANTHROPIC_API_KEY" not in env
        assert "PR_AGENT_CLAUDE_TOKEN_7_43" not in env
        return process

    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", launch)
    handler = claude_code.ClaudeCodeAIHandler(token="author-token")
    assert await handler.chat_completion("claude-sonnet-4-6", "system", "diff") == ("review", "stop")
    process.communicate.assert_awaited_once_with(b"diff")
