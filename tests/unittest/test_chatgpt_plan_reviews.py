import json
from copy import deepcopy

import httpx
import pytest
from starlette_context import request_cycle_context

from pr_agent.algo.ai_handlers import chatgpt_plan, claude_code
from pr_agent.algo.pr_processing import prefer_model, retry_with_fallback_models
from pr_agent.algo.run_details import init_run_details
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import _review_model_footer


@pytest.mark.parametrize("winner", ["plan", "claude", "api"])
async def test_chatgpt_plan_review_fallbacks(monkeypatch, winner):
    calls = []

    async def send(request):
        calls.append("plan")
        assert request.url == "https://chatgpt.com/backend-api/codex/responses"
        assert request.headers["authorization"] == "Bearer plan-token"
        assert request.headers["chatgpt-account-id"] == "account-1"
        payload = json.loads(request.content)
        assert payload == {
            "model": "gpt-5.6",
            "input": [{"role": "user", "content": "diff"}],
            "instructions": "system",
            "store": False,
            "stream": True,
        }
        if winner != "plan":
            return httpx.Response(401, json={"error": {"message": "rejected"}})
        response = {
            "id": "resp_test",
            "model": "gpt-5.6",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "review"}]}],
            "usage": None,
        }
        created = {"type": "response.created", "sequence_number": 0, "response": {**response, "output": []}}
        completed = {"type": "response.completed", "sequence_number": 1, "response": response}
        events = "".join(f"data: {json.dumps(event)}\n\n" for event in (created, completed))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=events)

    sdk = chatgpt_plan.openai.AsyncOpenAI

    def client(**kwargs):
        return sdk(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(send)))

    async def run_claude(*args, **kwargs):
        calls.append("claude")
        if winner == "api":
            raise RuntimeError("Claude unavailable")
        return "review", "stop"

    async def run_api(self, model, system, user, temperature, img_path):
        calls.append("api")
        assert model == "gpt-5.6"
        return "review", "stop"

    monkeypatch.setattr(chatgpt_plan.openai, "AsyncOpenAI", client)
    monkeypatch.setattr(claude_code, "_claude_code_completion", run_claude)
    monkeypatch.setattr(claude_code.LiteLLMAIHandler, "chat_completion", run_api)
    base_settings = get_settings()
    base_settings.get("config.ai_timeout")
    settings = deepcopy(base_settings)
    settings.set("config.model", "gpt-5.6")
    settings.set("config.fallback_models", [])
    with request_cycle_context({"settings": settings}):
        init_run_details()
        handler = chatgpt_plan.ChatGPTPlanAIHandler(
            access_token="plan-token", account_id="account-1", token="claude-token"
        )

        async def predict(model):
            return await handler.chat_completion(model, "system", "diff")

        with prefer_model("chatgpt-plan/gpt-5.6", "claude-sonnet-4-6"):
            assert await retry_with_fallback_models(predict) == ("review", "stop")
        footer = _review_model_footer()

    assert calls == ["plan", "claude", "api"][:["plan", "claude", "api"].index(winner) + 1]
    assert ("ChatGPT plan token" in footer) == (winner == "plan")
    assert ("Claude Code setup token" in footer) == (winner == "claude")
