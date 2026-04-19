"""Wiki cog — background ingest, curation loop, and slash commands.

Background tasks:
  - ingest_loop (every 30m): appends new discoveries.log entries to wiki/log.md
  - curation_loop (every 24h): calls local LLM to file log entries into topic pages
  - auto_promote_loop (every 1h): promotes drafts older than 24h to published

Slash commands:
  /wiki [action] [page]          — status, on/off, drafts, promote, reject, ingest
  /wiki-private [action] [page]  — manage private wiki tier
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.wiki import parse_curation_output

log = logging.getLogger("discord-nexus")
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from page content."""
    if not text.startswith("---"):
        return text
    match = re.search(r"\n---\s*\n", text)
    if match is None:
        match = re.search(r"\n---\s*$", text)
    if match is None:
        return text.lstrip("-").lstrip("\n")
    return text[match.end():].lstrip("\n")


_CURATION_PROMPT = """\
You are curating a project wiki. Below is the wiki index, followed by recent
log entries that haven't been filed into topic pages yet.

Your job:
1. Read the index to understand what pages exist.
2. For each log entry, decide: CREATE new page, UPDATE existing page, or SKIP
   (leave in log — not worth a page yet).
3. For CREATE/UPDATE: write the page content in markdown.
4. For SKIP: explain in one line why.

Rules:
- One fact per page update. Don't combine unrelated log entries.
- For UPDATE: preserve existing content — append or revise, don't replace.
- If uncertain, SKIP. The log entry persists and can be filed later.
- Never include: file paths, credentials, wallet addresses, private keys,
  account numbers, or PII. If a log entry contains these, SKIP it.
- Generate 2-5 keyword aliases for each CREATE/UPDATE (used for search).

Output format (repeat for each decision):

--- ACTION: create
PAGE: page-name-here
ALIASES: keyword1, keyword2, keyword3
CONTENT:
(page content in markdown)
--- END

--- ACTION: update
PAGE: existing-page-name
ALIASES: keyword1, keyword2
CONTENT:
(new/revised content to append or merge into existing page)
--- END

--- ACTION: skip
ENTRY: (copy of the log entry being skipped)
REASON: (one line explanation)
--- END

## Current Index
{index_content}

## Unfiled Log Entries
{unfiled_entries}
"""


class WikiCog(commands.Cog):
    """Periodically ingests discoveries.log and runs local LLM curation."""

    def __init__(self, bot) -> None:
        self.bot = bot
        data_dir = Path(getattr(bot, "data_dir", _DEFAULT_DATA_DIR))
        self._discoveries_path = data_dir / "discoveries.log"

    async def cog_check(self, ctx):
        return self.bot.allowlist.is_allowed(ctx.author.id)

    async def cog_load(self):
        if not getattr(self.bot, "wiki_enabled", False):
            return
        if getattr(self.bot, "wiki", None) is None:
            log.warning("wiki: wiki_enabled is true but bot.wiki is not configured")
            return
        if not self.ingest_loop.is_running():
            self.ingest_loop.start()
            log.info("wiki: ingest loop started (interval=30m)")
        if not self.curation_loop.is_running():
            self.curation_loop.start()
            log.info("wiki: curation loop started (interval=24h)")
        if not self.auto_promote_loop.is_running():
            self.auto_promote_loop.start()
            log.info("wiki: auto-promote loop started (interval=1h)")

    async def cog_unload(self):
        if self.ingest_loop.is_running():
            self.ingest_loop.cancel()
        if self.curation_loop.is_running():
            self.curation_loop.cancel()
        if self.auto_promote_loop.is_running():
            self.auto_promote_loop.cancel()

    @tasks.loop(minutes=30)
    async def ingest_loop(self):
        if not self._discoveries_path.exists():
            return
        wiki = getattr(self.bot, "wiki", None)
        if wiki is None:
            return
        try:
            count = await wiki.ingest_discoveries(self._discoveries_path)
        except Exception as exc:
            log.error("wiki: discovery ingest failed: %s", exc)
            return
        if count > 0:
            log.info("wiki: ingested %d new discovery entries", count)

    @ingest_loop.before_loop
    async def before_ingest_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def curation_loop(self):
        """Nightly curation: file unfiled log entries into topic pages."""
        if not getattr(self.bot, "wiki_enabled", False):
            return
        wiki = getattr(self.bot, "wiki", None)
        if wiki is None:
            return
        try:
            await self._curate_log_entries(wiki)
        except Exception as exc:
            log.error("wiki: curation loop failed: %s", exc)

    @curation_loop.before_loop
    async def before_curation_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def auto_promote_loop(self):
        """Hourly: promote drafts older than 24h to published."""
        if not getattr(self.bot, "wiki_enabled", False):
            return
        wiki = getattr(self.bot, "wiki", None)
        if wiki is None:
            return
        try:
            drafts = await wiki.list_drafts()
        except Exception as exc:
            log.error("wiki: auto-promote list failed: %s", exc)
            return

        now = datetime.now(timezone.utc)
        promoted = 0
        for draft in drafts:
            created_str = draft.get("created", "")
            if not created_str:
                continue
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now - created < timedelta(hours=24):
                continue
            try:
                if await wiki.promote_page(draft["name"]):
                    promoted += 1
                    log.info(
                        "wiki: auto-promoted draft %s (age: %s)",
                        draft["name"], now - created,
                    )
            except Exception as exc:
                log.error("wiki: auto-promote failed for %s: %s", draft["name"], exc)

        if promoted > 0:
            log.info("wiki: auto-promoted %d draft(s)", promoted)

    @auto_promote_loop.before_loop
    async def before_auto_promote_loop(self):
        await self.bot.wait_until_ready()

    async def _curate_log_entries(self, wiki) -> None:
        """Read unfiled log entries, call local LLM for curation, write drafts."""
        index_content = await wiki.read_index() or "(empty)"
        log_path = wiki.wiki_path / "log.md"
        if not log_path.exists():
            return

        log_text = log_path.read_text(encoding="utf-8")
        if not log_text.strip():
            return

        cursor_path = wiki.wiki_path / ".curation-cursor"
        cursor = 0
        if cursor_path.exists():
            try:
                cursor = int(cursor_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                cursor = 0

        if cursor > len(log_text):
            log.warning("wiki: curation cursor reset — log.md was truncated")
            cursor = 0

        unfiled = log_text[cursor:].strip()
        if not unfiled:
            log.info("wiki: curation skipped — no unfiled entries")
            return

        snapshot_end = len(log_text)

        prompt = _CURATION_PROMPT.format(
            index_content=index_content,
            unfiled_entries=unfiled,
        )

        # Call LM Studio directly for curation
        lmstudio_config = self.bot.config.get("lmstudio", {})
        api_url = f"{lmstudio_config.get('base_url', 'http://localhost:1234/v1')}/chat/completions"
        local_agent_config = self.bot.agent_configs.get("local-agent", {})
        model = local_agent_config.get("model", "")

        messages = [
            {"role": "system", "content": "You are a wiki curator."},
            {"role": "user", "content": prompt},
        ]

        raw_response = None
        try:
            raw_response = await self._call_local_llm(api_url, model, messages)
        except Exception as exc:
            log.error("wiki: curation LLM call failed: %s", exc)
            return

        if not raw_response:
            return

        actions = parse_curation_output(raw_response)

        if not actions:
            cursor_path.write_text(str(snapshot_end), encoding="utf-8")
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            fail_path = wiki.wiki_path / "drafts" / f"curation-failed-{date_str}.md"
            fail_path.parent.mkdir(parents=True, exist_ok=True)
            fail_path.write_text(
                f"# Curation parse failure\n\nRaw output:\n```\n{raw_response}\n```\n",
                encoding="utf-8",
            )
            log.error(
                "wiki: curation parse produced 0 actions, raw output saved to %s", fail_path
            )
            return

        wrote = 0
        failed_pages = []
        for action in actions:
            if action["action"] == "skip":
                continue
            page_name = action.get("page", "").lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*[a-z0-9]", page_name):
                log.warning("wiki: curation rejected invalid page name: %r", action.get("page"))
                failed_pages.append(action.get("page", "???"))
                continue
            try:
                content = action["content"]
                if action["action"] == "update":
                    existing = await wiki.read_page(page_name)
                    if existing:
                        body = _strip_frontmatter(existing)
                        content = body.rstrip() + "\n\n" + content
                await wiki.write_page(
                    page_name, content,
                    author="local-agent", source="curation",
                    status="draft", aliases=action.get("aliases", []),
                )
                wrote += 1
            except Exception as exc:
                log.error("wiki: curation failed to write page %s: %s", page_name, exc)
                failed_pages.append(page_name)

        cursor_path.write_text(str(snapshot_end), encoding="utf-8")

        if failed_pages:
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            fail_path = wiki.wiki_path / "drafts" / f"curation-failures-{date_str}.md"
            fail_path.parent.mkdir(parents=True, exist_ok=True)
            fail_path.write_text(
                f"# Curation failures — {date_str}\n\n"
                + "\n".join(f"- {p}" for p in failed_pages) + "\n",
                encoding="utf-8",
            )
            log.warning("wiki: %d page(s) failed, logged to %s", len(failed_pages), fail_path)

        if wrote > 0:
            log.info("wiki: curation wrote %d draft pages", wrote)
        elif not failed_pages:
            log.info("wiki: curation processed %d entries — all skipped", len(actions))

    async def _call_local_llm(
        self, url: str, model: str, messages: list[dict]
    ) -> str | None:
        """Direct LM Studio HTTP call for curation (bypasses agent abstraction)."""
        import os
        headers: dict[str, str] = {}
        api_key = os.getenv("LMSTUDIO_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(
                url,
                json={"messages": messages, "model": model, "temperature": 0.3, "max_tokens": 4096},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("wiki: LM Studio returned %d: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                usage = data.get("usage", {})
                log.info(
                    "wiki: curation call — model: %s, prompt: %d, completion: %d",
                    data.get("model", "unknown"),
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
                return data["choices"][0]["message"]["content"]

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="wiki", description="Manage the project wiki")
    @app_commands.describe(
        action="Action to perform",
        page="Page name (required for promote/reject)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="drafts", value="drafts"),
        app_commands.Choice(name="ingest", value="ingest"),
        app_commands.Choice(name="promote", value="promote"),
        app_commands.Choice(name="reject", value="reject"),
    ])
    async def slash_wiki(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] = None,
        page: str = "",
    ):
        await interaction.response.defer()
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.followup.send("Not authorized.", ephemeral=True)
            return
        action_val = action.value if action else "status"

        async def send(text: str):
            await interaction.followup.send(text)

        await self._wiki_dispatch(send, action_val, page)

    async def _wiki_dispatch(self, send, action: str, page: str = "") -> None:
        """Shared wiki action handler."""
        verb = action.lower()

        if verb == "off":
            self.bot.wiki_enabled = False
            if self.ingest_loop.is_running():
                self.ingest_loop.cancel()
            if self.curation_loop.is_running():
                self.curation_loop.cancel()
            if self.auto_promote_loop.is_running():
                self.auto_promote_loop.cancel()
            await send("Wiki disabled. All loops paused.")
            return

        if verb == "on":
            self.bot.wiki_enabled = True
            if getattr(self.bot, "wiki", None) is not None:
                if not self.ingest_loop.is_running():
                    self.ingest_loop.start()
                if not self.curation_loop.is_running():
                    self.curation_loop.start()
                if not self.auto_promote_loop.is_running():
                    self.auto_promote_loop.start()
            await send("Wiki enabled. All loops active.")
            return

        if verb == "status":
            state = "enabled" if getattr(self.bot, "wiki_enabled", False) else "disabled"
            wiki = getattr(self.bot, "wiki", None)
            page_count = "N/A"
            draft_count = "N/A"
            if wiki:
                pages_dir = wiki.wiki_path / "pages"
                drafts_dir = wiki.wiki_path / "drafts"
                if pages_dir.exists():
                    page_count = len(list(pages_dir.glob("*.md")))
                if drafts_dir.exists():
                    draft_count = len([
                        p for p in drafts_dir.glob("*.md")
                        if not p.name.startswith("curation-")
                    ])
            ingest = "running" if self.ingest_loop.is_running() else "stopped"
            await send(
                f"**Wiki:** {state} | **Pages:** {page_count} | "
                f"**Drafts:** {draft_count} | **Ingest:** {ingest}"
            )
            return

        if verb == "drafts":
            wiki = getattr(self.bot, "wiki", None)
            if wiki is None:
                await send("Wiki not configured.")
                return
            drafts = await wiki.list_drafts()
            if not drafts:
                await send("No drafts pending.")
                return
            lines = []
            now = datetime.now(timezone.utc)
            for d in drafts:
                age = ""
                if d["created"]:
                    try:
                        created = datetime.fromisoformat(d["created"].replace("Z", "+00:00"))
                        hours = int((now - created).total_seconds() / 3600)
                        age = f" ({hours}h ago)"
                    except ValueError:
                        pass
                lines.append(f"- `{d['name']}` by {d['source_agent']}{age}")
            await send("**Drafts:**\n" + "\n".join(lines))
            return

        if verb == "promote":
            if not page:
                await send("Usage: `/wiki promote page:<page-name>`")
                return
            wiki = getattr(self.bot, "wiki", None)
            if wiki is None:
                await send("Wiki not configured.")
                return
            if await wiki.promote_page(page.lower()):
                await send(f"Promoted `{page.lower()}` to published.")
            else:
                await send(f"No draft found for `{page.lower()}`.")
            return

        if verb == "reject":
            if not page:
                await send("Usage: `/wiki reject page:<page-name>`")
                return
            wiki = getattr(self.bot, "wiki", None)
            if wiki is None:
                await send("Wiki not configured.")
                return
            if await wiki.reject_page(page.lower()):
                await send(f"Rejected and deleted draft `{page.lower()}`.")
            else:
                await send(f"No draft found for `{page.lower()}`.")
            return

        if verb == "ingest":
            wiki = getattr(self.bot, "wiki", None)
            if wiki is None:
                await send("Wiki not configured.")
                return
            try:
                count = await wiki.ingest_discoveries(self._discoveries_path)
                await send(f"Ingested {count} new entries.")
            except Exception as exc:
                await send(f"Ingest failed: {exc}")
            return

        await send(
            "Usage: `/wiki action:[on|off|status|drafts|ingest|promote|reject] page:<name>`"
        )

    @app_commands.command(
        name="wiki-private",
        description="Manage the private wiki tier (local agent only)",
    )
    @app_commands.describe(
        action="Action to perform", page="Page name (required for promote/reject)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="drafts", value="drafts"),
        app_commands.Choice(name="promote", value="promote"),
        app_commands.Choice(name="reject", value="reject"),
    ])
    async def slash_wiki_private(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str] = None,
        page: str = "",
    ):
        await interaction.response.defer()
        if not self.bot.allowlist.is_allowed(interaction.user.id):
            await interaction.followup.send("Not authorized.", ephemeral=True)
            return
        action_val = action.value if action else "status"
        wiki = getattr(self.bot, "wiki", None)

        if action_val == "status":
            if wiki is None:
                await interaction.followup.send("Wiki not configured.")
                return
            private_dir = wiki.wiki_path / "private"
            pages_count = (
                len(list((private_dir / "pages").glob("*.md")))
                if (private_dir / "pages").exists()
                else 0
            )
            draft_count = (
                len(list((private_dir / "drafts").glob("*.md")))
                if (private_dir / "drafts").exists()
                else 0
            )
            await interaction.followup.send(
                f"**Private wiki:** Pages: {pages_count} | Drafts: {draft_count}\n"
                f"*Visible to local agent only. Not committed to git.*"
            )

        elif action_val == "drafts":
            if wiki is None:
                await interaction.followup.send("Wiki not configured.")
                return
            drafts = await wiki.list_private_drafts()
            if not drafts:
                await interaction.followup.send("No private drafts pending.")
                return
            now = datetime.now(timezone.utc)
            lines = []
            for d in drafts:
                age = ""
                if d["created"]:
                    try:
                        created = datetime.fromisoformat(d["created"].replace("Z", "+00:00"))
                        hours = int((now - created).total_seconds() / 3600)
                        age = f" ({hours}h ago)"
                    except ValueError:
                        pass
                lines.append(f"- `{d['name']}` by {d['source_agent']}{age}")
            await interaction.followup.send("**Private drafts:**\n" + "\n".join(lines))

        elif action_val == "promote":
            if not page:
                await interaction.followup.send(
                    "Usage: `/wiki-private promote page:<page-name>`"
                )
                return
            if wiki is None:
                await interaction.followup.send("Wiki not configured.")
                return
            if await wiki.promote_private_page(page.lower()):
                await interaction.followup.send(
                    f"Promoted private page `{page.lower()}` to published."
                )
            else:
                await interaction.followup.send(
                    f"No private draft found for `{page.lower()}`."
                )

        elif action_val == "reject":
            if not page:
                await interaction.followup.send(
                    "Usage: `/wiki-private reject page:<page-name>`"
                )
                return
            if wiki is None:
                await interaction.followup.send("Wiki not configured.")
                return
            if await wiki.reject_private_page(page.lower()):
                await interaction.followup.send(
                    f"Rejected and deleted private draft `{page.lower()}`."
                )
            else:
                await interaction.followup.send(
                    f"No private draft found for `{page.lower()}`."
                )

    @commands.command(name="wiki")
    async def wiki_cmd(self, ctx, action: str = "status", page: str = ""):
        """Wiki management: on/off, status, drafts, promote, reject, ingest."""
        await self._wiki_dispatch(ctx.send, action, page)


async def setup(bot):
    await bot.add_cog(WikiCog(bot))
