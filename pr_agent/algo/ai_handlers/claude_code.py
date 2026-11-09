import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.run_details import record_ai_call, record_model_source
from pr_agent.algo.token_budget import FallbackEligibleError
from pr_agent.config_loader import get_settings


class ClaudeCodeAIHandler(LiteLLMAIHandler):
    def __init__(self, *, token: str):
        super().__init__()
        self._token = token

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        claude_model = model.removeprefix("anthropic/")
        if not self._token or not claude_model.startswith("claude-") or "/" in claude_model:
            return await super().chat_completion(model, system, user, temperature, img_path)
        if img_path:
            raise FallbackEligibleError("The Claude Code setup-token backend supports text prompts only.")
        try:
            result, finish_reason = await _claude_code_completion(
                claude_model, system, user, token=self._token, timeout=get_settings().config.ai_timeout
            )
        except (RuntimeError, OSError) as error:
            raise FallbackEligibleError("Claude Code could not complete the request.") from error
        if finish_reason == "length":
            raise FallbackEligibleError("Claude Code output was truncated.")
        record_ai_call()
        record_model_source(model, claude_model, "Claude Code setup token")
        return result, finish_reason


async def _claude_code_completion(
    model: str, system: str, user: str, *, token: str, timeout: float
) -> tuple[str, str]:
    # Give the CLI its own working directory and only the environment needed to run.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL") if key in os.environ}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    # Claude Code thinks by default; a full review then needs several minutes and exceeds ai_timeout.
    env["MAX_THINKING_TOKENS"] = "0"

    with TemporaryDirectory(prefix="pr-agent-claude-") as directory:
        env["CLAUDE_CONFIG_DIR"] = str(Path(directory) / "config")
        system_prompt = Path(directory) / "system.txt"
        system_prompt.write_text(system, encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                "--safe-mode",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--tools", "",
                "--max-turns", "1",
                "--output-format", "json",
                "--system-prompt-file", str(system_prompt),
                f"--model={model}",
                cwd=directory,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeError("Install Claude Code and make 'claude' available on PATH.") from None

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(user.encode("utf-8")), timeout)
        except (TimeoutError, asyncio.CancelledError):
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise

    if process.returncode:
        raise RuntimeError(f"Claude Code exited with status {process.returncode}. Check the setup token and CLI.")
    try:
        response = json.loads(stdout)
    except ValueError:
        raise RuntimeError("Claude Code returned invalid JSON.") from None
    if not isinstance(response, dict) or response.get("is_error") or response.get("subtype") != "success":
        raise RuntimeError("Claude Code did not complete the request successfully.")
    result = response.get("result")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Claude Code returned an empty completion.")
    return result, "length" if response.get("stop_reason") == "max_tokens" else "stop"
