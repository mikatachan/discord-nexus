# discord-nexus

A modular Discord bot framework for connecting AI agents — Claude Code CLI, Codex CLI, and local LLMs (LM Studio, Ollama, vLLM) — to Discord as a collaborative multi-agent system.

---

## What It Is

discord-nexus lets you run multiple AI agents in a Discord server where they can:

- Respond to messages via role mention (`@Claude`, `@Local Agent`, `@Codex`) or slash command
- Respond to `@team <prompt>` to call all agents simultaneously
- Hand off tasks to each other with a simple `@AgentName <task>` protocol
- Maintain per-thread conversation history and agent workspaces
- Write to a shared wiki (public and private tiers)
- Post discoveries to a shared channel
- Trigger web research tasks

Each agent posts as a distinct Discord user via webhook, with its own name and avatar.

---

## Architecture

```
Discord Message
      │
      ▼
  bot.py (NexusBot)
      │
      ├── routing/dispatcher.py
      │     Determines which agent(s) to invoke
      │
      ├── cogs/agents.py
      │     Orchestrates agent calls, tag processing, handoffs, webhooks
      │
      ├── agents/
      │     ├── cli.py          ClaudeAgent, CodexAgent (subprocess)
      │     ├── local_llm.py    LocalLLMAgent (HTTP, OpenAI-compatible)
      │     ├── openclaw_relay.py  OpenClawRelayAgent (optional gateway)
      │     └── researcher.py   ResearcherAgent (optional, web research)
      │
      ├── services/wiki.py      Flat-file wiki with public + private tiers
      ├── persistence/db.py     SQLite (aiosqlite) — history, jobs, workspaces
      └── cogs/
            ├── utility.py      /help, /monitor, /dashboard, /restart, slash agents
            └── wiki.py         /wiki, /wiki-private, auto-ingest loop
```

Agent output is scanned for structured tags (`<!-- DISCOVERY: -->`, `<!-- WIKI: -->`, etc.)
before being chunked and posted to Discord.

---

## Quickstart

### 1. Prerequisites

- Python 3.11+
- A Discord application and bot token ([discord.com/developers](https://discord.com/developers))
- At least one of:
  - [Claude Code CLI](https://docs.anthropic.com/claude-code) (`npm install -g @anthropic-ai/claude-code`)
  - [Codex CLI](https://github.com/openai/codex) (`npm install -g @openai/codex`)
  - A local LLM server (LM Studio, Ollama, vLLM) running on `http://localhost:1234`

### 2. Clone and install

```bash
git clone https://github.com/your-org/discord-nexus.git
cd discord-nexus
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

Edit `.env` — fill in your Discord bot token.
Edit `config.yaml` — set your server ID, channel IDs, agent roles, and which agents to enable.

See [`docs/platform-setup.md`](docs/platform-setup.md) for a full walkthrough.

### 4. Run

```bash
python bot.py
```

For persistent operation with auto-restart, see the PM2 setup in [`docs/platform-setup.md`](docs/platform-setup.md).

### 5. Invite the bot

In the Discord Developer Portal, enable the **Message Content Intent** and generate an invite URL with:
- `bot` scope
- `applications.commands` scope
- Permissions: Send Messages, Manage Webhooks, Read Message History, Embed Links, Add Reactions

---

## Features

| Feature | Description |
|---|---|
| Multi-agent routing | Role mentions and slash commands route to the correct agent |
| @team broadcast | Mention a configurable team role to call all agents in parallel |
| Agent handoffs | Agents hand off tasks to each other via `@AgentName <task>` |
| Per-thread history | Conversation history stored per thread per agent in SQLite |
| Agent workspaces | Per-thread scratch state preserved across turns |
| Public wiki | Shared Markdown wiki, written by agents or users |
| Private wiki | Separate tier for sensitive content, stored outside the repo |
| Discoveries | Agents post notable findings to a shared channel |
| Web research | Optional researcher agent triggers search queries |
| Secret redaction | Output is scanned for secrets before posting |
| Health dashboard | `/dashboard` posts a live-updating embed with agent status |
| Rate-limit fallback | If Claude is rate-limited, falls back to Codex, then local LLM |
| Cross-platform | Windows and Mac/Linux supported |
| PM2 ready | `ecosystem.config.js` included for persistent operation |

---

## Agents

| Agent | Type | Required |
|---|---|---|
| `claude` | Claude Code CLI subprocess | Optional |
| `codex` | Codex CLI subprocess | Optional |
| `local-agent` | Local LLM (OpenAI-compatible HTTP) | Optional |
| `openclaw` | OpenClaw gateway relay | Optional |
| `researcher` | Web research via OpenClaw | Optional |

At least one agent must be configured and online. See [`docs/agents.md`](docs/agents.md).

---

## Documentation

- [Architecture](docs/architecture.md) — system diagram, data flow, component overview
- [Agents](docs/agents.md) — configuring each agent type, adding custom agents
- [Wiki System](docs/wiki-system.md) — wiki structure, tags, private tier, curation
- [Platform Setup](docs/platform-setup.md) — Windows and Mac/Linux install guides, PM2

---

## Security Notes

- Bot token and API keys are read from `.env` — never commit this file
- Private wiki pages live in `wiki/private/` (gitignored — never committed); `PRIVATE_DB_PATH` controls where the private SQLite DB is stored
- On Windows, the private DB directory is hardened with `icacls` on first run
- All agent output is scanned for secrets before posting to Discord
- The allowlist controls who can use `/restart` and other privileged commands

---

## Support

If you find this useful, donations are appreciated:

- **BTC:** `bc1qyqx8eqlzpjvp3nnmgpfltq5p5vj43z5tqt553y`
- **SOL:** `FxM3HmqJFNErRr3MFiPbAL9ojpuActaQ1h6TfH9fUPs2`
- **ETH:** `0x55BF0d4a4185F6905268E503f4E64ecc5fB8538f`

---

## Acknowledgements

The optional `OpenClawRelayAgent` is designed to work with [OpenClaw](https://github.com/Light-Heart-Labs/DreamServer), the agent framework included in **Dream Server** by [Light Heart Labs](https://github.com/Light-Heart-Labs). Dream Server is a fully local AI stack (LLM inference, agents, voice, workflows, RAG) deployable on your own hardware with a single command — and a natural companion to discord-nexus if you want a complete self-hosted setup.

---

## License

MIT
