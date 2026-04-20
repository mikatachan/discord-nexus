"""Cron job cog — schedule recurring agent prompts via cron expressions.

Usage:
  /cron add name:"daily-brief" schedule:"0 8 * * *" agent:claude prompt:"Summarize activity"
  /cron list
  /cron disable name:"daily-brief"
  /cron enable  name:"daily-brief"
  /cron delete  name:"daily-brief"

Requires: croniter >= 2.0 (pip install croniter)
"""

import asyncio
import logging
import time

import discord
from croniter import croniter
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)


class CronCog(commands.Cog, name="Cron"):
    """Schedule agent prompts on a recurring cron schedule."""

    cron_group = app_commands.Group(name="cron", description="Manage scheduled agent jobs")

    def __init__(self, bot):
        self.bot = bot
        self.cron_tick.start()

    def cog_unload(self):
        self.cron_tick.cancel()

    # ── Scheduler ─────────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def cron_tick(self):
        """Check for due cron jobs every minute and dispatch them."""
        try:
            now = time.time()
            jobs = await self.bot.db.get_due_cron_jobs(now)
            for job in jobs:
                await self._fire_job(job, now)
        except Exception as e:
            log.error("Cron tick error: %s", e)

    @cron_tick.before_loop
    async def before_cron_tick(self):
        await self.bot.wait_until_ready()

    async def _fire_job(self, job: dict, now: float):
        """Fire a single job: update timestamps, then dispatch to agent."""
        try:
            itr = croniter(job["schedule"], now)
            next_run = itr.get_next(float)
        except Exception as e:
            log.error("Cron job %s: invalid schedule %r: %s", job["name"], job["schedule"], e)
            return

        # Update timestamps first — prevents double-fire if bot restarts mid-dispatch
        await self.bot.db.update_cron_job_run(job["id"], now, next_run)

        channel = self.bot.get_channel(job["channel_id"])
        if not channel:
            log.warning("Cron job %s: channel %d not found", job["name"], job["channel_id"])
            return

        agents_cog = self.bot.get_cog("Agents")
        if not agents_cog:
            log.error("Cron job %s: Agents cog not loaded", job["name"])
            return

        log.info(
            "Cron job %s: firing → %s in #%s (next: %d)",
            job["name"], job["agent_name"], getattr(channel, "name", job["channel_id"]), int(next_run),
        )
        asyncio.create_task(
            agents_cog.handle_agent_request(
                agent_name=job["agent_name"],
                prompt=job["prompt"],
                thread_id=str(job["channel_id"]),
                channel=channel,
                user_id=job["created_by"],
            )
        )

    # ── Slash commands ─────────────────────────────────────────────────────────

    @cron_group.command(name="add", description="Schedule a recurring agent prompt")
    @app_commands.describe(
        name="Unique job name (e.g. 'daily-brief')",
        schedule="Cron expression — e.g. '0 8 * * *' for 8 AM daily (UTC)",
        agent="Agent to call (see your config for valid agent names)",
        prompt="Prompt to send to the agent on each run",
    )
    async def cron_add(
        self,
        interaction: discord.Interaction,
        name: str,
        schedule: str,
        agent: str,
        prompt: str,
    ):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        agent = agent.lower().strip()
        if agent not in self.bot.agents:
            names = ", ".join(f"`{k}`" for k in self.bot.agents)
            await interaction.response.send_message(
                f"Unknown agent `{agent}`. Valid: {names}", ephemeral=True
            )
            return

        if not croniter.is_valid(schedule):
            await interaction.response.send_message(
                f"Invalid cron expression: `{schedule}`\n"
                "Format: `minute hour day month weekday`\n"
                "Examples: `0 8 * * *` (8 AM daily) · `*/30 * * * *` (every 30 min) · `0 9 * * 1` (9 AM Mondays)",
                ephemeral=True,
            )
            return

        try:
            itr = croniter(schedule, time.time())
            next_run = itr.get_next(float)
            await self.bot.db.create_cron_job(
                name=name,
                schedule=schedule,
                channel_id=interaction.channel_id,
                agent_name=agent,
                prompt=prompt,
                created_by=interaction.user.id,
                next_run=next_run,
            )
        except Exception as e:
            if "UNIQUE constraint" in str(e):
                await interaction.response.send_message(
                    f"A job named `{name}` already exists. Delete it first with `/cron delete`.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(f"Error creating job: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ Cron job **{name}** created.\n"
            f"Schedule: `{schedule}` — next run: <t:{int(next_run)}:f>\n"
            f"Agent: `{agent}` | Channel: <#{interaction.channel_id}>"
        )

    @cron_group.command(name="list", description="List all scheduled cron jobs")
    async def cron_list(self, interaction: discord.Interaction):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        jobs = await self.bot.db.list_cron_jobs()
        if not jobs:
            await interaction.response.send_message("No cron jobs configured.", ephemeral=True)
            return

        lines = ["**Scheduled Cron Jobs**"]
        for job in jobs:
            icon = "✅" if job["enabled"] else "⏸️"
            last = f"<t:{int(job['last_run'])}:R>" if job["last_run"] else "never"
            snippet = job["prompt"][:80] + ("…" if len(job["prompt"]) > 80 else "")
            lines.append(
                f"{icon} **{job['name']}** — `{job['schedule']}` → `{job['agent_name']}` in <#{job['channel_id']}>\n"
                f"  Last: {last} · Next: <t:{int(job['next_run'])}:f>\n"
                f"  Prompt: {snippet}"
            )

        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1897] + "…"
        await interaction.response.send_message(body)

    @cron_group.command(name="delete", description="Delete a scheduled cron job")
    @app_commands.describe(name="Name of the job to delete")
    async def cron_delete(self, interaction: discord.Interaction, name: str):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        deleted = await self.bot.db.delete_cron_job(name)
        if deleted:
            await interaction.response.send_message(f"🗑️ Cron job **{name}** deleted.")
        else:
            await interaction.response.send_message(f"No job named `{name}` found.", ephemeral=True)

    @cron_group.command(name="enable", description="Re-enable a paused cron job")
    @app_commands.describe(name="Name of the job to enable")
    async def cron_enable(self, interaction: discord.Interaction, name: str):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        updated = await self.bot.db.set_cron_job_enabled(name, True)
        if updated:
            await interaction.response.send_message(f"▶️ Cron job **{name}** enabled.")
        else:
            await interaction.response.send_message(f"No job named `{name}` found.", ephemeral=True)

    @cron_group.command(name="disable", description="Pause a cron job without deleting it")
    @app_commands.describe(name="Name of the job to disable")
    async def cron_disable(self, interaction: discord.Interaction, name: str):
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        updated = await self.bot.db.set_cron_job_enabled(name, False)
        if updated:
            await interaction.response.send_message(f"⏸️ Cron job **{name}** paused.")
        else:
            await interaction.response.send_message(f"No job named `{name}` found.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CronCog(bot))
