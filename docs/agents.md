# Agents

## Overview

discord-nexus supports four agent types. None are required — configure only what you have.

| Agent | Class | Backend | Required |
|---|---|---|---|
| `claude` | `ClaudeAgent` | Claude Code CLI | No |
| `codex` | `CodexAgent` | Codex CLI | No |
| `local-agent` (or any name) | `LocalLLMAgent` | OpenAI-compatible HTTP | No |
| `openclaw` | `OpenClawRelayAgent` | OpenClaw gateway | No |
| `researcher` | `ResearcherAgent` | OpenClaw researcher workspace | No |

At least one agent must be running for the bot to respond.

---

## ClaudeAgent

Invokes `claude` as a subprocess (Claude Code CLI).

### Install

```bash
npm install -g @anthropic-ai/claude-code
claude login   # authenticate with your Anthropic account
```

### Config (`config.yaml`)

```yaml
claude:
  timeout: 120
  system_prompt: "You are Claude, a helpful AI assistant..."
```

### How it works

- Spawns `claude -p --output-format json` as a subprocess; prompt is passed via stdin
- On Windows, suppresses the console window via `CREATE_NO_WINDOW`
- `CLAUDE.md` in the project root is auto-loaded by the CLI as context
- Environment variables like `DISCORD_TOKEN` are stripped before the subprocess inherits env

### Notes

- Claude Code CLI requires Node.js 18+
- Authentication is managed by `claude login` — credentials stored in `~/.claude`
- Rate limits surface as `AgentRateLimitError` which triggers the fallback chain

---

## CodexAgent

Invokes `codex` as a subprocess (OpenAI Codex CLI).

### Install

```bash
npm install -g @openai/codex
# Set OPENAI_API_KEY in your .env
```

### Config (`config.yaml`)

```yaml
codex:
  timeout: 120
  system_prompt: "You are Codex, an AI assistant specializing in code..."
```

### How it works

- Spawns `codex exec --skip-git-repo-check --sandbox danger-full-access --ephemeral -` as a subprocess; prompt is passed via stdin
- On Windows, uses `codex.cmd` and suppresses the console window
- Strips sensitive env vars before subprocess execution (same as ClaudeAgent)

### Notes

- Requires `OPENAI_API_KEY` in the environment
- Codex CLI respects a `CODEX.md` in the project root (similar to `CLAUDE.md`)

---

## LocalLLMAgent

Calls any OpenAI-compatible HTTP endpoint directly. Works with LM Studio, Ollama, vLLM, and others.

### Prerequisites

Start your local server before running the bot:

**LM Studio:**
```
Settings → Local Server → Start Server (default port 1234)
```

**Ollama:**
```bash
ollama serve
ollama pull llama3  # or any model
```

**vLLM:**
```bash
python -m vllm.entrypoints.openai.api_server --model <model-name>
```

### Config (`config.yaml`)

```yaml
lmstudio:
  base_url: "http://localhost:1234/v1"
  model: "local-model"      # must match what your server reports
  timeout: 60
  max_tokens: 2048
  temperature: 0.7
  api_key: ""               # leave empty for LM Studio / Ollama (no auth needed)
  system_prompt: |
    You are a helpful local AI assistant...
```

The `lmstudio` config key is used by default. You can rename it — just update `bot.py` to match.

### health_check

Probes `GET /models` on the base URL. If the endpoint responds and at least one model is listed,
the agent is marked online.

---

## OpenClawRelayAgent (Optional)

Relays requests to an OpenClaw gateway — a self-hosted proxy that can front multiple backends.

This agent is **optional** and useful if you run an OpenClaw instance that manages model routing,
context injection, or workspace management independently of the bot.

### Config (`config.yaml`)

```yaml
openclaw:
  enabled: false          # set true to activate
  base_url: "http://localhost:8080"
  workspace: "discord"
  timeout: 60
  stream: true
  system_prompt: "..."
```

Set `OPENCLAW_GATEWAY_TOKEN` in `.env` for authentication.

---

## ResearcherAgent (Optional)

Extends `OpenClawRelayAgent` to perform web research. Triggered by the `<!-- RESEARCH: query -->` tag
in any agent's response.

Requires OpenClaw to be configured and running with a researcher workspace.

### Config (`config.yaml`)

```yaml
researcher:
  enabled: false
  workspace: "researcher"
  # inherits openclaw base_url and token
```

When triggered:
1. The bot calls `researcher.call(query)`
2. The researcher agent fetches and sanitizes web content
3. Results are returned as a follow-up message in the same thread

---

## Agent Loading in bot.py

The bot loads agents on startup based on what's enabled in `config.yaml`.

The default loading logic in `bot.py`:

```python
# Option A: Use OpenClawRelayAgent for local LLM (via gateway)
# Option B: Use LocalLLMAgent directly (no gateway needed)
#
# Uncomment the block that matches your setup.
```

Both options are present in `bot.py` as commented sections. Pick one and uncomment it.

---

## Adding a Custom Agent

1. Create `agents/my_agent.py` extending `BaseAgent`:

```python
from agents.base import BaseAgent

class MyAgent(BaseAgent):
    def __init__(self, config: dict):
        super().__init__(name="myagent", config=config)

    async def call(self, prompt: str, system_prompt: str = "", thread_id: str = "") -> str:
        # Your implementation here
        ...

    async def health_check(self) -> dict:
        return {"status": "ok", "model": "my-model"}

    async def close(self):
        pass
```

2. Register in `bot.py`:

```python
from agents.my_agent import MyAgent

if config.get("myagent", {}).get("enabled"):
    self.agents["myagent"] = MyAgent(config["myagent"])
```

3. Add to `routing/dispatcher.py`:

```python
ALL_AGENTS = ["claude", "codex", "local-agent", "myagent"]
```

4. Add a role in `config.yaml`:

```yaml
agent_roles:
  myagent: YOUR_ROLE_ID
```

---

## Rate-Limit Fallback

If `claude` raises `AgentRateLimitError`, the bot tries agents in this order:

```
claude → codex → local-agent
```

This fallback chain is defined in `cogs/agents.py`. Adjust it to match your enabled agents.

---

## Agent Identity in Discord

Each agent posts as a distinct webhook user. Configure display names and avatar URLs in `config.yaml`:

```yaml
agents:
  claude:
    webhook_name: "Claude"
    webhook_avatar: "https://example.com/claude-avatar.png"
  codex:
    webhook_name: "Codex"
    webhook_avatar: "https://example.com/codex-avatar.png"
  local-agent:
    webhook_name: "Local Agent"
    webhook_avatar: "https://example.com/local-agent-avatar.png"
```

The bot creates webhooks in each registered channel automatically on first use.
