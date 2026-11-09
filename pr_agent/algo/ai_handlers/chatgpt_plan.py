import openai

from pr_agent.algo.ai_handlers.claude_code import ClaudeCodeAIHandler
from pr_agent.algo.run_details import record_ai_call, record_model_source
from pr_agent.algo.token_budget import CHATGPT_PLAN_MODEL_PREFIX, FallbackEligibleError
from pr_agent.config_loader import get_settings

# ChatGPT plan OAuth tokens are accepted by the Codex backend, not by the public API base URL.
CHATGPT_PLAN_BASE_URL = "https://chatgpt.com/backend-api/codex"


class ChatGPTPlanAIHandler(ClaudeCodeAIHandler):
    def __init__(self, *, access_token: str, account_id: str, token: str):
        super().__init__(token=token)
        self._plan_access_token = access_token
        self._plan_account_id = account_id

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        if not model.startswith(CHATGPT_PLAN_MODEL_PREFIX):
            return await super().chat_completion(model, system, user, temperature, img_path)
        if img_path:
            raise FallbackEligibleError("ChatGPT plan reviews currently support text prompts only.")

        model_id = model.removeprefix(CHATGPT_PLAN_MODEL_PREFIX)
        try:
            async with openai.AsyncOpenAI(
                api_key=self._plan_access_token,
                base_url=CHATGPT_PLAN_BASE_URL,
                organization="",
                project="",
                default_headers={"ChatGPT-Account-Id": self._plan_account_id, "originator": "pr_agent"},
                timeout=get_settings().config.ai_timeout,
                max_retries=0,
            ) as client:
                async with client.responses.stream(
                    model=model_id,
                    instructions=system,
                    input=[{"role": "user", "content": user}],
                    store=False,
                ) as stream:
                    response = await stream.get_final_response()
        except (openai.OpenAIError, RuntimeError, ValueError):
            # SDK errors may contain credentials or the provider's response body.
            raise FallbackEligibleError("ChatGPT plan inference failed.") from None

        if response.status != "completed" or not response.output_text.strip():
            raise FallbackEligibleError("ChatGPT plan inference returned no complete review.")
        usage = response.usage
        record_ai_call({
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
        } if usage else None)
        record_model_source(model, response.model or model_id, "ChatGPT plan token")
        return response.output_text, "stop"
