"""discord-nexus — main entry point.

A modular Discord multi-agent bot framework connecting Claude Code CLI,
Codex CLI, and local LLMs (LM Studio, Ollama, etc.) to Discord.

Setup:
  1. Copy config.yaml.example → config.yaml and fill in your values
  2. Copy .env.example → .env and set DISCORD_TOKEN
  3. pip install -r requirements.txt
  4. python bot.py

See docs/ for full setup instructions.
"""

import asyncio
import atexit
import json
import logging
import os
from pathlib import Path
import sys
import time

import discord
import yaml
from discord.ext import commands as discord_commands
from dotenv import load_dotenv

from agents.cli import ClaudeAgent, CodexAgent
from persistence.db import (
    Database,
    _PRIVATE_SCHEMA,
    get_repo_root,
    get_shared_db_path,
    resolve_private_db_path,
)
from security.allowlist import Allowlist
from security.filter import load_secret_literals
from utils.log import setup_logging

# --- Setup ---

load_dotenv(get_repo_root() / ".env")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
setup_logging(LOG_DIR)
log = logging.getLogger("discord-nexus")

# Load config
with open("config.yaml") as f:
    config = yaml.safe_load(f)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN or DISCORD_TOKEN == "your-discord-token-here":
    raise SystemExit("DISCORD_TOKEN not set in .env — see .env.example")

LMSTUDIO_API_KEY = os.getenv("LMSTUDIO_API_KEY") or None
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN") or None

# Load actual secret values for literal matching in output filter
load_secret_literals()

# --- Intents ---

intents = discord.Intents.default()
intents.message_content = True


# --- Bot ---

class NexusBot(discord_commands.Bot):
    """Main bot class for discord-nexus.

    Loads agents from config.yaml and exposes them to cogs.
    Add new agents in the __init__ agents dict below.
    """

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.config = config
        self.db = Database(get_shared_db_path())
        self.private_db = None
        self.allowlist = Allowlist(config["bot"]["allowed_users"])

        # Discord mention for the primary user (first in allowed_users)
        _uid = config["bot"]["allowed_users"][0] if config["bot"]["allowed_users"] else None
        self.alert_mention = f"<@{_uid}>" if _uid else ""

        # Agent configuration blocks from config.yaml
        self.agent_configs = {
            "local-agent": config.get("local-agent", {}),
            "claude": config.get("claude", {}),
            "codex": config.get("codex", {}),
            "researcher": config.get("researcher", {}),
        }

        # --- Agent backends ---
        # To add or remove agents, edit this dict and agent_configs above.
        # Each agent must implement BaseAgent (agents/base.py).
        self.agents: dict = {}

        # Claude Code CLI (required for /claude)
        self.agents["claude"] = ClaudeAgent(
            timeout=config.get("claude", {}).get("timeout", 120),
        )

        # Codex CLI (required for /codex)
        self.agents["codex"] = CodexAgent(
            timeout=config.get("codex", {}).get("timeout", 120),
        )

        # Local LLM agent — choose your backend:
        # Option A: OpenClaw relay (if you run an OpenClaw gateway)
        if config.get("openclaw") and OPENCLAW_GATEWAY_TOKEN:
            from agents.openclaw_relay import OpenClawRelayAgent
            self.agents["local-agent"] = OpenClawRelayAgent(
                base_url=config["openclaw"]["base_url"],
                agent_id=config["openclaw"]["agent_id"],
                timeout=config["openclaw"]["timeout"],
                auth_token=OPENCLAW_GATEWAY_TOKEN,
            )
        # Option B: Direct LocalLLMAgent (LM Studio, Ollama, etc.)
        elif config.get("lmstudio"):
            from agents.local_llm import LocalLLMAgent
            self.agents["local-agent"] = LocalLLMAgent(
                base_url=config["lmstudio"]["base_url"],
                model=config.get("local-agent", {}).get("model", ""),
                timeout=config["lmstudio"]["timeout"],
                api_key=LMSTUDIO_API_KEY,
            )
            # Override the default agent name to match the config display name
            self.agents["local-agent"].name = config.get("local-agent", {}).get("display_name", "Local Agent")

        # Researcher agent (optional — requires OpenClaw with researcher workspace)
        if config.get("openclaw") and OPENCLAW_GATEWAY_TOKEN:
            from agents.researcher import ResearcherAgent
            self.agents["researcher"] = ResearcherAgent(
                base_url=config["openclaw"]["base_url"],
                timeout=config.get("researcher", {}).get("timeout", 120),
                auth_token=OPENCLAW_GATEWAY_TOKEN,
            )

        # Hook point: add more custom agents here
        # Example:
        # from agents.my_custom_agent import MyAgent
        # self.agents["myagent"] = MyAgent(...)

        # Per-agent active channels (populated from config.yaml *_channels lists)
        self.agent_channels = {
            "local-agent": set(config.get("local-agent_channels", [])),
            "claude": set(config.get("claude_channels", [])),
            "codex": set(config.get("codex_channels", [])),
            "researcher": set(config.get("researcher_channels", [])),
        }

        # Agent role IDs for @mention routing
        self._agent_role_ids: dict[str, int] = {
            k: int(v) for k, v in config.get("agent_roles", {}).items()
        }

        ch = config.get("channels", {})
        self.alerts_channel_id = ch.get("alerts", 0) or 0
        self.discoveries_channel_id = ch.get("discoveries", 0) or 0
        self.handoffs_channel_id = ch.get("handoffs", 0) or 0

        self.log_config = config.get("logging", {})
        self.conv_config = config.get("conversation", {})
        self._projects: dict = config.get("projects", {})
        self._channel_projects: dict = {
            str(k): v for k, v in config.get("channel_projects", {}).items()
        }

        self._webhooks: dict[tuple[int, str], discord.Webhook] = {}
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._log_last_sent = 0.0
        self._agent_status: dict[str, bool] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # Wiki
        wiki_config = config.get("wiki", {})
        self.wiki_enabled = wiki_config.get("enabled", False)
        if self.wiki_enabled:
            from services.wiki import WikiStore
            wiki_path = Path(wiki_config.get("path", "wiki"))
            if not wiki_path.is_absolute():
                wiki_path = Path(__file__).resolve().parent / wiki_path
            # pinned_pages: must exist at wiki/pages/<slug>.md before startup
            pinned_pages = wiki_config.get("pinned_pages", [])
            try:
                self.wiki = WikiStore(wiki_path, pinned_pages=pinned_pages)
            except RuntimeError as e:
                log.warning("wiki: initialization failed — %s. Wiki disabled.", e)
                self.wiki = None
                self.wiki_enabled = False
        else:
            self.wiki = None

        self.data_dir = Path(DATA_DIR)

    def _get_lock(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._thread_locks:
            self._thread_locks[thread_id] = asyncio.Lock()
        return self._thread_locks[thread_id]

    def _get_channel_mission(self, channel_id: str, agent_name: str) -> str:
        """Return the mission string for an agent in a channel (from config.yaml)."""
        missions = config.get("channel_missions", {})
        channel_missions = missions.get(str(channel_id), {})
        return channel_missions.get(agent_name, "")

    async def _post_to_alerts(self, message: str):
        """Post a message to #alerts, optionally mentioning the primary user."""
        if not self.alerts_channel_id:
            return
        channel = self.get_channel(self.alerts_channel_id)
        if channel:
            prefix = f"{self.alert_mention} " if self.alert_mention else ""
            await channel.send(f"{prefix}`[ALERT]` {message}")

    async def _post_discovery(self, finding: str, source_agent: str):
        """Post a discovery to #discoveries and append to data/discoveries.log."""
        if finding:
            import datetime
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            log.info("Discovery from %s: %s", source_agent, finding)
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "discoveries.log"), "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{source_agent}] {finding}\n")
        if self.discoveries_channel_id and finding:
            channel = self.get_channel(self.discoveries_channel_id)
            if channel:
                await channel.send(
                    f"**{source_agent.capitalize()} discovery:**\n{finding}"
                )

    async def _handle_new_channel(self, message) -> None:
        """!new-channel [agents] — register current channel with specified agents.

        Usage:
          !new-channel             — all agents (local-agent, claude, codex)
          !new-channel local-agent      — local-agent only
          !new-channel claude codex — specific agents
        """
        if not self.allowlist.is_allowed(message.author.id):
            return
        channel = message.channel
        cid = channel.id

        content = message.content.strip()
        args = content[len("!new-channel"):].strip().lower().split()
        valid = {"local-agent", "claude", "codex"}
        targets = [a for a in args if a in valid] or list(valid)

        # Update live sets immediately
        for agent in targets:
            if agent in self.agent_channels:
                self.agent_channels[agent].add(cid)

        # Persist to config.yaml
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            with open(config_path, encoding="utf-8") as f:
                raw = f.read()
            import yaml as _yaml
            cfg = _yaml.safe_load(raw)
            changed = []
            for agent in targets:
                key = f"{agent}_channels"
                lst = cfg.get(key, [])
                if cid not in lst:
                    lst.append(cid)
                    cfg[key] = lst
                    changed.append(agent)
            if changed:
                with open(config_path, "w", encoding="utf-8") as f:
                    _yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
            label = ", ".join(changed) if changed else "already registered"
            await channel.send(
                f"<#{cid}> registered for: **{', '.join(targets)}**\n"
                f"{'Config updated.' if changed else 'No config change needed.'} "
                f"Use `!restart` to fully reload."
            )
        except Exception as e:
            log.error("!new-channel config write failed: %s", e)
            await channel.send(
                f"Live registration ok, but config write failed: `{e}`\n"
                f"Add channel ID `{cid}` manually to config.yaml."
            )

    # --- Lifecycle ---

    async def setup_hook(self):
        """Called by discord.py before the bot connects. Sets up DB, loads cogs."""
        await self.db.connect()
        log.info("Shared DB: %s", get_shared_db_path().resolve())

        # Private DB setup (optional — for private memories outside the repo)
        try:
            private_db_path = resolve_private_db_path()
        except RuntimeError as e:
            log.info("Private DB not configured: %s — private memory disabled", e)
            self.private_db = None
        else:
            private_db_path.parent.mkdir(parents=True, exist_ok=True)

            # Windows-only: harden directory permissions with icacls
            if sys.platform == "win32":
                try:
                    import subprocess as _subprocess
                    _subprocess.run(
                        [
                            "icacls",
                            str(private_db_path.parent),
                            "/inheritance:r",
                            "/grant:r",
                            f"{os.environ.get('USERNAME', 'Users')}:(OI)(CI)F",
                        ],
                        check=False,
                        capture_output=True,
                    )
                except Exception as _e:
                    log.warning("icacls hardening failed (non-fatal): %s", _e)

            self.private_db = Database(
                private_db_path,
                schema_sql=_PRIVATE_SCHEMA,
                run_shared_migrations=False,
            )
            await self.private_db.connect()
            log.info("Private DB: %s", private_db_path.resolve())

        # Write PID file for external monitoring
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "bot.pid"), "w") as f:
            f.write(str(os.getpid()))
        log.info("PID %d written to data/bot.pid", os.getpid())

        await self.db.recover_stale_jobs()

        # --- Load cogs ---
        # Core cogs (always loaded)
        cog_modules = [
            "cogs.agents",    # Agent dispatch and webhooks
            "cogs.utility",   # /help, /monitor, /dashboard, agent slash commands
            "cogs.wiki",      # Wiki management and background curation
        ]

        # Hook point: add more cogs here
        # Example: "cogs.my_custom_cog"

        for module in cog_modules:
            try:
                await self.load_extension(module)
            except Exception as e:
                log.error("Failed to load cog %s: %s", module, e)

        await self.tree.sync()

        # Health check all configured agents
        for name, agent in self.agents.items():
            health = await agent.health_check()
            if health["status"] == "ok":
                log.info("%s online — %s", name.capitalize(), health.get("model", ""))
                self._agent_status[name] = True
            else:
                log.warning(
                    "%s OFFLINE — %s", name.capitalize(), health.get("error", "unknown")
                )
                self._agent_status[name] = False

        # Start background maintenance tasks
        self._cleanup_task = asyncio.create_task(self._audit_cleanup_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def close(self):
        """Graceful shutdown — closes DB connections and agent HTTP sessions."""
        log.info("Shutting down...")
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for agent in self.agents.values():
            if hasattr(agent, "close"):
                await agent.close()
        if self.private_db is not None:
            await self.private_db.close()
        await self.db.close()
        try:
            os.remove(os.path.join(DATA_DIR, "bot.pid"))
        except FileNotFoundError:
            pass
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Serving %d guilds", len(self.guilds))
        status_parts = []
        for name, online in self._agent_status.items():
            status_parts.append(f"{name.capitalize()}: {'online' if online else 'OFFLINE'}")
        await self._log_to_channel(f"Bot started. {', '.join(status_parts)}")

        # Post restart confirmation if this was triggered by !restart
        flag_path = os.path.join(DATA_DIR, "restart_flag.json")
        if os.path.exists(flag_path):
            try:
                with open(flag_path) as f:
                    flag = json.load(f)
                os.remove(flag_path)
                ch = self.get_channel(flag["channel_id"])
                if ch:
                    await ch.send("Back online.")
            except Exception:
                pass

    # --- Message handling ---

    async def on_message(self, message: discord.Message):
        # Ignore webhook messages and other bots
        if message.webhook_id or message.author.bot:
            return

        # Allowlist check — only authorized users can interact
        if not self.allowlist.is_allowed(message.author.id):
            return

        # Persist message for conversation history
        thread_id = str(message.channel.id)
        await self.db.save_message(
            thread_id,
            "user",
            f"[{message.author.display_name}]: {message.content}",
            author_id=str(message.author.id),
            message_id=str(message.id),
        )

        # Handle prefix commands (e.g. !wiki, !monitor)
        ctx = await self.get_context(message)
        if ctx.valid:
            await self.invoke(ctx)
            return

        # !restart is handled here because it exits the process
        if message.content.strip().lower() == "!restart":
            await message.channel.send("Restarting...")
            flag_path = os.path.join(DATA_DIR, "restart_flag.json")
            with open(flag_path, "w") as f:
                import json as _json
                _json.dump({"channel_id": message.channel.id}, f)
            sys.exit(0)

        # Dispatch agent commands (!bang, @role mentions)
        agents_cog = self.get_cog("Agents")
        if agents_cog and await agents_cog.dispatch_agents(message):
            return

    # --- Logging ---

    async def _log_to_channel(self, message: str):
        """Send a log message to #logs, rate-limited."""
        log_channel_id = self.log_config.get("channel_id")
        rate_limit = self.log_config.get("rate_limit", 5)
        if not log_channel_id:
            return
        now = time.time()
        if now - self._log_last_sent < rate_limit:
            log.info("[log-channel-throttled] %s", message)
            return
        try:
            channel = self.get_channel(log_channel_id)
            if channel:
                await channel.send(f"`[LOG]` {message}")
                self._log_last_sent = now
        except Exception as e:
            log.warning("Failed to send to #logs: %s", e)

    # --- Maintenance ---

    async def _heartbeat_loop(self):
        """Write heartbeat timestamp every 5 minutes for external watchdog monitoring."""
        while True:
            try:
                tmp = os.path.join(DATA_DIR, "heartbeat.txt.tmp")
                with open(tmp, "w") as f:
                    json.dump({"ts": time.time()}, f)
                os.replace(tmp, os.path.join(DATA_DIR, "heartbeat.txt"))
            except Exception as e:
                log.warning("Heartbeat write failed: %s", e)
            await asyncio.sleep(300)

    async def _audit_cleanup_loop(self):
        """Daily cleanup of old audit log entries and archived conversations."""
        while True:
            try:
                await asyncio.sleep(86400)
                retention = config.get("retention", {}).get("audit_days", 30)
                await self.db.cleanup_audit(retention)
                conv_retention = config.get("retention", {}).get("conversation_days", 7)
                await self.db.archive_old_conversations(conv_retention)
                await self.db.cleanup_old_workspaces(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Audit cleanup failed: %s", e)


# --- Entry point ---

bot = NexusBot()


def _shutdown():
    """atexit handler — best-effort cleanup on Windows process exit."""
    loop = getattr(bot, "loop", None)
    try:
        is_running = loop and loop.is_running()
    except AttributeError:
        return
    if is_running:
        loop.create_task(bot.close())


atexit.register(_shutdown)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
