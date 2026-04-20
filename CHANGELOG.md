# Changelog

All notable changes to discord-nexus are documented here.

---

## [0.1.0] — 2026-04-19

Initial public release.

### Features

- Multi-agent routing via role mentions (`@Agent`) and slash commands (`/claude`, `/codex`, `/local-agent`)
- `@team` role broadcast — mention a configurable role to call all agents in parallel
- Agent handoffs — agents delegate to each other via `@AgentName <task>` in responses
- Per-thread conversation history in SQLite (aiosqlite)
- Per-thread agent workspaces (scratch notes preserved across turns)
- Live streaming — Claude and Codex stream partial output to a Discord placeholder as they generate
- Thread support — webhook routing works correctly in forum posts and thread channels
- `/stop` — cancel a running agent mid-generation
- Cron scheduler — `/cron add|list|delete|enable|disable` for recurring agent prompts
- Public wiki — flat-file Markdown wiki with auto-ingest and agent write tags
- Private wiki — separate tier for sensitive content, gitignored and stored outside the repo
- Memory washer (`washer.py`) — nightly pipeline that extracts durable memories from conversation history using a local LLM
- Private review queue — sensitive memory extractions held for manual approval
- Discoveries — agents post notable findings to a shared channel via `<!-- DISCOVERY: -->` tags
- Web research — optional researcher agent for search queries
- Secret redaction — all agent output scanned before posting to Discord
- Rate-limit fallback — Claude rate-limited → falls back to Codex → local LLM
- Health dashboard — `/dashboard` posts a live-updating embed with agent and system status
- Cross-platform — Windows (codex.cmd, CREATE_NO_WINDOW) and Mac/Linux supported
- PM2 ready — `ecosystem.config.js` included for persistent operation with auto-restart
