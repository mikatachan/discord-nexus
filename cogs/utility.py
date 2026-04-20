"""Utility commands — slash commands, health dashboard, agent status.

Provides:
  /help      — full command list
  /monitor   — agent health and token usage
  /dashboard — auto-updating health embed
  /discover  — post a finding to #discoveries
  /claude, /codex, /local-agent, /research — slash command entry points for agents
  /new-channel — register current channel with agents
  /restart   — restart the bot process
"""

import asyncio
import json
import logging
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.fake_message import FakeMessage

log = logging.getLogger(__name__)

_bot_start_time: float | None = None


class Utility(commands.Cog):
    """Slash commands and utility operations."""

    def __init__(self, bot):
        self.bot = bot
        self._dashboard_message: discord.Message | None = None
        self._monitor_last_called: dict[int, float] = {}
        global _bot_start_time
        _bot_start_time = time.monotonic()

    async def get_status(self) -> str:
        """Build a status string showing agent health and token usage."""
        bot_name = self.bot.config.get("bot", {}).get("name", "YourBot")
        lines = [f"**{bot_name} Status**"]

        if _bot_start_time is not None:
            elapsed = time.monotonic() - _bot_start_time
            hours, rem = divmod(int(elapsed), 3600)
            mins, secs = divmod(rem, 60)
            lines.append(f"Uptime: {hours}h {mins}m {secs}s")

        lines.append("")

        for name, agent in self.bot.agents.items():
            health = await agent.health_check()
            if health["status"] == "ok":
                self.bot._agent_status[name] = True
                model_info = health.get("model", "")
                totals = await self.bot.db.get_token_totals_24h(name)
                stats_parts = []
                if totals["tokens_input"] or totals["tokens_output"]:
                    stats_parts.append(
                        f"in:{totals['tokens_input']:,} out:{totals['tokens_output']:,}"
                    )
                if totals["cost_usd"]:
                    stats_parts.append(f"${totals['cost_usd']:.4f}")
                lines.append(f"- {name.capitalize()}: Online (`{model_info}`)")
                if stats_parts:
                    lines.append(f"  24h tokens: {', '.join(stats_parts)}")
            else:
                self.bot._agent_status[name] = False
                lines.append(
                    f"- {name.capitalize()}: OFFLINE ({health.get('error', 'unknown')})"
                )

        lines.append("- Database: Connected")
        return "\n".join(lines)

    @commands.command(name="help")
    async def help_command(self, ctx):
        bot_name = self.bot.config.get("bot", {}).get("name", "YourBot")
        await ctx.send(
            f"**{bot_name} commands (use `/help` for the full slash command list):**\n"
            "\n"
            "**Agents** — `@Claude`, `@Local Agent`, `@Codex` role mentions or "
            "`/claude`, `/local-agent`, `/codex`, `/research`\n"
            "**Wiki** — `/wiki [action] [page]`\n"
            "**Utility** — `/monitor`, `/dashboard`, `/discover`, `/new-channel`, `/restart`"
        )

    @commands.command(name="monitor")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def monitor(self, ctx):
        status = await self.get_status()
        await ctx.send(status)

    @commands.command(name="discover")
    async def discover(self, ctx, *, finding: str = ""):
        if finding:
            await self.bot._post_discovery(finding, "user")
            await ctx.message.add_reaction("📌")

    @commands.command(name="new-channel")
    async def new_channel(self, ctx, *, args: str = ""):
        await self.bot._handle_new_channel(ctx.message)

    # --- Slash commands for agents ---

    @app_commands.command(name="local-agent", description="Ask the local LLM agent a question")
    @app_commands.describe(prompt="Your question or prompt")
    async def slash_local_agent(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer()
        await interaction.followup.send(f"**{interaction.user.display_name}:** {prompt}")
        await self.bot.handle_agent_request(
            agent_name="local-agent",
            prompt=prompt,
            thread_id=str(interaction.channel_id),
            channel=interaction.channel,
            user_id=interaction.user.id,
        )

    @app_commands.command(name="claude", description="Ask Claude a question")
    @app_commands.describe(prompt="Your question or prompt for Claude")
    async def slash_claude(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer()
        await interaction.followup.send(f"**{interaction.user.display_name}:** {prompt}")
        await self.bot.handle_agent_request(
            agent_name="claude",
            prompt=prompt,
            thread_id=str(interaction.channel_id),
            channel=interaction.channel,
            user_id=interaction.user.id,
        )

    @app_commands.command(name="codex", description="Ask Codex a question")
    @app_commands.describe(prompt="Your question or prompt for Codex")
    async def slash_codex(self, interaction: discord.Interaction, prompt: str):
        await interaction.response.defer()
        await interaction.followup.send(f"**{interaction.user.display_name}:** {prompt}")
        await self.bot.handle_agent_request(
            agent_name="codex",
            prompt=prompt,
            thread_id=str(interaction.channel_id),
            channel=interaction.channel,
            user_id=interaction.user.id,
        )

    @app_commands.command(name="research", description="Send a web research query (requires researcher agent)")
    @app_commands.describe(query="What to research")
    async def slash_research(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        await interaction.followup.send(f"**{interaction.user.display_name}:** {query}")
        await self.bot.handle_agent_request(
            agent_name="researcher",
            prompt=query,
            thread_id=str(interaction.channel_id),
            channel=interaction.channel,
            user_id=interaction.user.id,
        )

    # --- Utility slash commands ---

    @app_commands.command(name="monitor", description="Check bot and agent status")
    async def slash_monitor(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        now = time.monotonic()
        last = self._monitor_last_called.get(guild_id, 0)
        if now - last < 30:
            await interaction.response.send_message(
                f"Status was just posted — please wait {30 - int(now - last)}s.",
                ephemeral=True,
            )
            return
        self._monitor_last_called[guild_id] = now
        await interaction.response.defer()
        status = await self.get_status()
        await interaction.followup.send(status)

    @app_commands.command(name="help", description="Show all available commands")
    async def slash_help(self, interaction: discord.Interaction):
        bot_name = self.bot.config.get("bot", {}).get("name", "YourBot")
        await interaction.response.send_message(
            f"**{bot_name} commands:**\n"
            "\n"
            "**Agents**\n"
            "`@Claude <msg>` — Claude (role mention)\n"
            "`@Local Agent <msg>` — local LLM agent (role mention)\n"
            "`@Codex <msg>` — Codex (role mention)\n"
            "`/local-agent <prompt>` — slash command for local LLM agent\n"
            "`/claude <prompt>` — slash command for Claude\n"
            "`/codex <prompt>` — slash command for Codex\n"
            "`/research <query>` — web research (requires researcher agent)\n"
            "\n"
            "**Wiki**\n"
            "`/wiki [action] [page]` — manage the project wiki\n"
            "`/wiki-private [action] [page]` — private wiki (local agent only)\n"
            "\n"
            "**Utility**\n"
            "`/monitor` — agent health and token usage\n"
            "`/dashboard` — auto-updating health embed\n"
            "`/discover <finding>` — post to #discoveries\n"
            "`/new-channel [agents]` — register channel with agents\n"
            "`/restart` — restart the bot",
            ephemeral=True,
        )

    @app_commands.command(name="discover", description="Post a finding to #discoveries")
    @app_commands.describe(finding="The discovery to record")
    async def slash_discover(self, interaction: discord.Interaction, finding: str):
        await interaction.response.defer(ephemeral=True)
        await self.bot._post_discovery(finding, "user")
        await interaction.followup.send("Discovery posted!", ephemeral=True)

    @app_commands.command(
        name="new-channel", description="Register current channel with agents"
    )
    @app_commands.describe(agents="Agent names to register (leave empty for all)")
    async def slash_new_channel(
        self, interaction: discord.Interaction, agents: str = ""
    ):
        await interaction.response.defer(ephemeral=True)
        content = f"!new-channel {agents}".strip()
        fake = FakeMessage(
            content, interaction.channel, interaction.user.id, interaction.guild
        )
        await self.bot._handle_new_channel(fake)
        await interaction.followup.send("Done.", ephemeral=True)

    @app_commands.command(name="dashboard", description="Post the auto-updating health dashboard")
    async def slash_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self._build_dashboard_embed()
        self._dashboard_message = await interaction.followup.send(embed=embed)
        if not self._dashboard_loop.is_running():
            self._dashboard_loop.start()
        await interaction.channel.send(
            "Dashboard posted. It will auto-update every 60 seconds."
        )

    @app_commands.command(name="stop", description="Stop the agent currently running in this channel")
    async def slash_stop(self, interaction: discord.Interaction):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        agents_cog = self.bot.get_cog("Agents")
        if not agents_cog:
            await interaction.response.send_message("Agents cog not loaded.", ephemeral=True)
            return
        channel_key = str(interaction.channel_id)
        agent = agents_cog._active_agents.get(channel_key)
        if agent is None:
            await interaction.response.send_message("No agent is running in this channel.", ephemeral=True)
            return
        await interaction.response.send_message(f"Stopping {agent.name}...", ephemeral=True)
        agents_cog._active_agents.pop(channel_key, None)
        await agent.kill()

    @app_commands.command(name="restart", description="Restart the bot process")
    async def slash_restart(self, interaction: discord.Interaction):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        await interaction.response.send_message("Restarting...")
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
        flag_path = os.path.join(data_dir, "restart_flag.json")
        with open(flag_path, "w") as f:
            json.dump({"channel_id": interaction.channel_id}, f)
        await asyncio.sleep(0.5)
        sys.exit(0)

    # --- Health dashboard ---

    async def _build_dashboard_embed(self) -> discord.Embed:
        """Build a rich embed with agent health information."""
        bot_name = self.bot.config.get("bot", {}).get("name", "YourBot")
        embed = discord.Embed(
            title=f"{bot_name} Health Dashboard",
            color=discord.Color.green(),
        )

        if _bot_start_time is not None:
            elapsed = time.monotonic() - _bot_start_time
            hours, rem = divmod(int(elapsed), 3600)
            mins, _ = divmod(rem, 60)
            embed.add_field(name="Uptime", value=f"{hours}h {mins}m", inline=True)

        all_ok = True
        for name, agent in self.bot.agents.items():
            health = await agent.health_check()
            if health["status"] == "ok":
                self.bot._agent_status[name] = True
                model = health.get("model", "?")
                embed.add_field(
                    name=name.capitalize(), value=f"Online\n`{model}`", inline=True
                )
            else:
                self.bot._agent_status[name] = False
                all_ok = False
                embed.add_field(
                    name=name.capitalize(),
                    value=f"OFFLINE\n{health.get('error', '?')[:50]}",
                    inline=True,
                )

        if not all_ok:
            embed.color = discord.Color.red()

        embed.set_footer(text=f"Updated: {time.strftime('%H:%M:%S')}")
        return embed

    @commands.command(name="dashboard")
    async def dashboard_cmd(self, ctx):
        """Post the health dashboard embed (auto-updates every 60s)."""
        embed = await self._build_dashboard_embed()
        self._dashboard_message = await ctx.send(embed=embed)
        if not self._dashboard_loop.is_running():
            self._dashboard_loop.start()
        await ctx.send("Dashboard posted. It will auto-update every 60 seconds.")

    @tasks.loop(seconds=60)
    async def _dashboard_loop(self):
        """Update the pinned dashboard embed every 60 seconds."""
        if not self._dashboard_message:
            return
        try:
            embed = await self._build_dashboard_embed()
            await self._dashboard_message.edit(embed=embed)
        except discord.NotFound:
            log.info("Dashboard message deleted — stopping loop")
            self._dashboard_message = None
            self._dashboard_loop.stop()
        except Exception as e:
            log.warning("Dashboard update failed: %s", e)

    def cog_unload(self):
        if self._dashboard_loop.is_running():
            self._dashboard_loop.cancel()


async def setup(bot):
    cog = Utility(bot)
    await bot.add_cog(cog)
    bot.get_status = cog.get_status
