"""CLI-based agents — Claude Code and Codex via subprocess.

Supports both Windows (codex.cmd) and Mac/Linux (codex) automatically.
Agents run as subprocesses and receive the prompt via stdin.
"""

import asyncio
import json as _json
import logging
import os
import re
import subprocess
import sys

from .base import AgentOfflineError, AgentRateLimitError, AgentTimeoutError, BaseAgent

# On Windows, npm-installed CLIs require the .cmd extension for subprocess_exec.
_IS_WIN = sys.platform == "win32"
_CODEX_CMD = "codex.cmd" if _IS_WIN else "codex"
# Suppress console windows on Windows (no effect on Mac/Linux)
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

log = logging.getLogger(__name__)

# Patterns that indicate a usage/rate limit rather than a generic offline error
_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "too many requests",
    "overloaded",
    "plan limit",
    "monthly limit",
    "weekly limit",
    "session limit",
]

# Environment keys that must never propagate to subprocess agents
_STRIP_ENV_KEYS: frozenset[str] = frozenset({
    "DISCORD_TOKEN",
    "PRIVATE_DB_PATH",
    "OPENAI_API_KEY",
})


def _filtered_env() -> dict[str, str]:
    """Return os.environ minus keys that must not reach subprocess agents."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}


def _is_rate_limit(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _RATE_LIMIT_PATTERNS)


class ClaudeAgent(BaseAgent):
    """Claude Code CLI agent via `claude -p`.

    Requires the Claude Code CLI to be installed and authenticated:
      npm install -g @anthropic-ai/claude-code
      claude auth
    """

    def __init__(self, timeout: int = 120, work_dir: str | None = None, model: str | None = None):
        super().__init__(name="Claude", timeout=timeout)
        self.work_dir = work_dir
        self.model = model  # Optional: pin to specific model e.g. "claude-sonnet-4-6"

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
        work_dir: str | None = None,
        timeout: int | None = None,
    ) -> tuple[str, dict]:
        """Call Claude Code CLI with conversation history + system prompt."""
        prompt = self._build_prompt(messages, system_prompt, mission=mission, workspace=workspace)
        effective_dir = work_dir or self.work_dir
        effective_timeout = timeout or self.timeout

        try:
            cmd = ["claude", "-p", "--output-format", "json"]
            if self.model:
                cmd += ["--model", self.model]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_dir,
                env=_filtered_env(),
                **_NO_WINDOW,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()),
                timeout=effective_timeout,
            )

            output = stdout.decode().strip()
            metadata = {}
            response_text = output

            if proc.returncode != 0:
                err = stderr.decode().strip()
                log.error("Claude CLI error (code %d): %s", proc.returncode, err)
                if not output:
                    if _is_rate_limit(err) or _is_rate_limit(output):
                        raise AgentRateLimitError(f"Claude usage/rate limit: {err[:200]}")
                    raise AgentOfflineError(f"Claude CLI failed: {err[:200]}")

            try:
                data = _json.loads(output)
                response_text = data.get("result", output)
                if data.get("is_error") and _is_rate_limit(response_text):
                    raise AgentRateLimitError(f"Claude usage/rate limit: {response_text[:200]}")
                metadata = {
                    "tokens_input": data.get("usage", {}).get("input_tokens"),
                    "tokens_output": data.get("usage", {}).get("output_tokens"),
                    "tokens_cache_read": data.get("usage", {}).get("cache_read_input_tokens"),
                    "cost_usd": data.get("total_cost_usd"),
                }
            except (_json.JSONDecodeError, KeyError):
                log.warning("Claude output was not JSON — token data unavailable")

            log.info(
                "Claude — chars: %d, cost: $%.4f, exit: %d",
                len(response_text),
                metadata.get("cost_usd") or 0,
                proc.returncode,
            )
            return response_text, metadata

        except FileNotFoundError:
            raise AgentOfflineError(
                "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise AgentTimeoutError(f"Claude did not respond within {effective_timeout}s")

    async def health_check(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            return {"status": "ok", "model": f"Claude Code ({version})"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _build_prompt(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
    ) -> str:
        """Build 4-layer prompt: IDENTITY → MISSION → SCRATCH → HISTORY."""
        parts = []

        # Layer 1: IDENTITY (system prompt)
        parts.append(system_prompt)

        # Layer 2: MISSION (per-channel north star, if any)
        if mission:
            parts.append(f"\n## MISSION\n{mission}")

        # Layer 3: SCRATCH (agent's own working notes from previous turns)
        if workspace:
            parts.append(f"\n## [{self.name} working notes]\n{workspace}")

        parts.append("")  # blank line separator before history

        # Layer 4: HISTORY
        for msg in messages:
            role = msg["role"].upper()
            parts.append(f"{role}: {msg['content']}")

        return "\n".join(parts)


class CodexAgent(BaseAgent):
    """Codex CLI agent via stdin pipe to `codex exec`.

    Requires the Codex CLI to be installed and authenticated:
      npm install -g @openai/codex
      codex auth     (or set OPENAI_API_KEY)

    Windows note: the CLI is installed as codex.cmd — this is handled automatically.
    """

    def __init__(self, timeout: int = 120, work_dir: str | None = None):
        super().__init__(name="Codex", timeout=timeout)
        self.work_dir = work_dir
        self._current_proc: asyncio.subprocess.Process | None = None

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
        work_dir: str | None = None,
        timeout: int | None = None,
    ) -> tuple[str, dict]:
        """Call Codex CLI with conversation history + system prompt."""
        prompt = self._build_prompt(messages, system_prompt, mission=mission, workspace=workspace)
        effective_dir = work_dir or self.work_dir
        effective_timeout = timeout or self.timeout

        cmd_args = [
            _CODEX_CMD, "exec",
            "--skip-git-repo-check",
            "--sandbox", "danger-full-access",
            "--ephemeral",
            "-",  # read prompt from stdin
        ]
        if effective_dir:
            cmd_args.insert(2, "-C")
            cmd_args.insert(3, effective_dir)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_dir,
                env=_filtered_env(),
                **_NO_WINDOW,
            )
            self._current_proc = proc

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()),
                timeout=effective_timeout,
            )

            response_text = stdout.decode().replace("\r\n", "\n").replace("\r", "\n").strip()
            err_output = stderr.decode().replace("\r\n", "\n").replace("\r", "\n")

            if proc.returncode != 0 and not response_text:
                log.error("Codex CLI error (code %d): %s", proc.returncode, err_output[:200])
                if _is_rate_limit(err_output) or _is_rate_limit(response_text):
                    raise AgentRateLimitError(f"Codex usage/rate limit: {err_output[:200]}")
                raise AgentOfflineError(f"Codex CLI failed: {err_output[:200]}")

            tokens_used = self._extract_tokens(err_output)
            metadata = {"tokens_output": tokens_used or None}
            log.info(
                "Codex — chars: %d, tokens: %d, exit: %d",
                len(response_text), tokens_used, proc.returncode,
            )
            return response_text, metadata

        except FileNotFoundError:
            raise AgentOfflineError(
                f"Codex CLI not found ({_CODEX_CMD}). Install: npm install -g @openai/codex"
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise AgentTimeoutError(f"Codex did not respond within {effective_timeout}s")
        finally:
            self._current_proc = None

    async def kill(self) -> None:
        """Kill the currently running subprocess if any. No-op if idle."""
        if self._current_proc is not None:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    async def health_check(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CODEX_CMD, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            return {"status": "ok", "model": f"Codex ({version})"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _build_prompt(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
    ) -> str:
        """Build 4-layer prompt: IDENTITY → MISSION → SCRATCH → HISTORY."""
        parts = []

        # Layer 1: IDENTITY
        parts.append(system_prompt)

        # Layer 2: MISSION
        if mission:
            parts.append(f"\n## MISSION\n{mission}")

        # Layer 3: SCRATCH
        if workspace:
            parts.append(f"\n## [{self.name} working notes]\n{workspace}")

        parts.append("")

        # Layer 4: HISTORY
        for msg in messages:
            role = msg["role"].upper()
            parts.append(f"{role}: {msg['content']}")

        return "\n".join(parts)

    def _extract_tokens(self, stderr_output: str) -> int:
        """Extract the token count from Codex stderr output."""
        tok_match = re.search(r"\ntokens used\n([\d,]+)", stderr_output)
        if tok_match:
            return int(tok_match.group(1).replace(",", ""))
        return 0
