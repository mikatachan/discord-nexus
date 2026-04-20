"""Agent commands — !bang dispatch, @role routing, webhook identity, handoffs.

This cog handles all agent interactions:
  - !claude / !c  → Claude Code CLI
  - !codex / !g   → Codex CLI
  - !local-agent / !m  → Local LLM (via OpenClaw relay or LocalLLMAgent)
  - !all / !a     → Broadcasts to all agents
  - @AgentRole    → Discord role mention routing

Processed tags (stripped before Discord output):
  <!-- SCRATCH -->...</>        — agent working memory (per-thread)
  <!-- DISCOVERY: ... -->       — posts to #discoveries channel
  <!-- WIKI: name -->...</>     — writes to shared wiki
  <!-- WIKI-PRIVATE: name -->...</> — writes to private wiki tier (local agent only)
  <!-- RESEARCH: query -->      — triggers researcher agent follow-up
  @AgentName ...               — handoff to another agent
"""

import asyncio
import json
import logging
import re
import time

import discord
from discord.ext import commands

from agents.base import AgentOfflineError, AgentRateLimitError, AgentTimeoutError
from routing.dispatcher import ALL_AGENTS, parse_commands, resolve_channel_id, should_respond
from security.filter import scan_output
from utils.chunker import chunk_message
from utils.log import set_correlation, clear_correlation

log = logging.getLogger("discord-nexus")


def build_discord_context(alert_mention: str | None, mission: str, wiki_context: str) -> str:
    """Build the [Discord Context] block appended to relay messages for local agents."""
    parts = ["[Discord Context]"]
    if alert_mention:
        parts.append(f"alert_mention: {alert_mention}")
    if mission:
        parts.append(f"mission: {mission}")
    if wiki_context:
        parts.append(f"wiki_context:\n{wiki_context}")
    return "\n".join(parts)


def _format_memory_block(memories: list[dict]) -> str:
    """Format a list of memory dicts into a plain-text block for prompt injection."""
    lines = []
    for m in memories:
        lines.append(f"- [{m['type']}] {m['content']}")
    return "\n".join(lines)


class Agents(commands.Cog):
    """Agent interaction: direct chat, handoffs, webhook identity."""

    MAX_HANDOFF_DEPTH = 4

    # Matches both legacy !bang and new @Agent handoff lines in agent responses
    _HANDOFF_RE = re.compile(
        r"^(?:!(?:local-agent|m|claude|c|codex|g)|@(?:local-agent|claude|codex))\b\s*(.*)",
        re.IGNORECASE | re.MULTILINE,
    )

    # Maps @AgentName text to internal agent name
    _MENTION_AGENT_MAP = {
        "local-agent": "local-agent",
        "claude": "claude",
        "codex": "codex",
    }

    def __init__(self, bot):
        self.bot = bot

    async def dispatch_agents(self, message: discord.Message) -> bool:
        """Handle agent @role mentions and !bang prefix dispatch.

        Returns True if any agent was dispatched (consumed the message).
        """
        # --- @team → all agents in parallel ---
        team_role_id = getattr(self.bot, "_team_role_id", None)
        if team_role_id and message.role_mentions:
            if any(r.id == team_role_id for r in message.role_mentions):
                content = message.content
                for role in message.role_mentions:
                    content = content.replace(role.mention, "")
                prompt = content.strip()
                if not prompt:
                    await message.channel.send("Usage: @team <your question>")
                    return True
                channel_id = resolve_channel_id(message.channel)
                active_agents = [
                    a for a in ALL_AGENTS
                    if should_respond(channel_id, self.bot.agent_channels.get(a, set()))
                ]
                if active_agents:
                    thread_id = str(message.channel.id)
                    await asyncio.gather(*[
                        self.handle_agent_request(
                            agent_name=agent_name,
                            prompt=prompt,
                            thread_id=thread_id,
                            channel=message.channel,
                            user_id=message.author.id,
                        )
                        for agent_name in active_agents
                    ])
                return True

        # --- @role mention routing ---
        agent_role_ids = getattr(self.bot, "_agent_role_ids", {})
        if agent_role_ids and message.role_mentions:
            role_agents: list[str] = []
            for agent_name, role_id in agent_role_ids.items():
                for role_mention in message.role_mentions:
                    if role_mention.id == role_id:
                        role_agents.append(agent_name)
                        break
            if role_agents:
                # Strip all role mentions from content to get the prompt
                content = message.content
                for mention in message.role_mentions:
                    content = content.replace(mention.mention, "")
                prompt = content.strip()
                if not prompt:
                    names = " / ".join(f"@{a.capitalize()}" for a in role_agents)
                    await message.channel.send(f"Usage: @{names} <your question>")
                    return True
                channel_id = resolve_channel_id(message.channel)
                active_agents = [
                    a for a in role_agents
                    if should_respond(channel_id, self.bot.agent_channels.get(a, set()))
                ]
                inactive = [a for a in role_agents if a not in active_agents]
                if inactive:
                    names = ", ".join(a.capitalize() for a in inactive)
                    await message.channel.send(f"{names} isn't active in this channel.")
                if active_agents:
                    thread_id = str(message.channel.id)
                    await asyncio.gather(*[
                        self.handle_agent_request(
                            agent_name=agent_name,
                            prompt=prompt,
                            thread_id=thread_id,
                            channel=message.channel,
                            user_id=message.author.id,
                        )
                        for agent_name in active_agents
                    ])
                return True

        # --- !bang command routing ---
        agents, prompt = parse_commands(message.content)
        if not agents:
            return False

        if not prompt:
            names = ", ".join(f"`!{a}`" for a in agents)
            await message.channel.send(f"Usage: {names} <your question>")
            return True

        channel_id = resolve_channel_id(message.channel)
        active_agents = []
        inactive_agents = []
        for agent_name in agents:
            agent_chs = self.bot.agent_channels.get(agent_name, set())
            if should_respond(channel_id, agent_chs):
                active_agents.append(agent_name)
            else:
                inactive_agents.append(agent_name)

        if inactive_agents:
            names = ", ".join(a.capitalize() for a in inactive_agents)
            await message.channel.send(f"{names} isn't active in this channel.")

        if not active_agents:
            return True

        thread_id = str(message.channel.id)
        tasks = [
            self.handle_agent_request(
                agent_name=agent_name,
                prompt=prompt,
                thread_id=thread_id,
                channel=message.channel,
                user_id=message.author.id,
            )
            for agent_name in active_agents
        ]
        await asyncio.gather(*tasks)
        return True

    def _resolve_work_dir(self, prompt: str, channel) -> tuple[str, str | None]:
        """Strip --project flag from prompt and return (cleaned_prompt, work_dir).

        Resolution order:
          1. --project <name> flag in prompt (stripped before forwarding to agent)
          2. channel_projects config mapping for the current channel
          3. None (agent uses no CWD override)

        Hook point: customize project resolution logic here if needed.
        """
        projects = getattr(self.bot, "_projects", {})
        channel_projects = getattr(self.bot, "_channel_projects", {})

        proj_match = re.search(r"--project\s+(\S+)", prompt)
        if proj_match:
            project_name = proj_match.group(1)
            prompt = re.sub(r"\s*--project\s+\S+", "", prompt).strip()
        else:
            channel_id_str = str(resolve_channel_id(channel))
            project_name = channel_projects.get(channel_id_str)

        work_dir = projects.get(project_name, {}).get("path") if project_name else None
        return prompt, work_dir

    async def handle_agent_request(
        self,
        agent_name: str,
        prompt: str,
        thread_id: str,
        channel,
        user_id: int,
        depth: int = 0,
        source_agent: str | None = None,
        *,
        ephemeral_context: str = "",
        work_dir: str | None = None,
        message_id: str | None = None,
        origin_already_persisted: bool = False,
    ):
        """Handle a request for any agent.

        This is the central dispatch point. It:
          1. Resolves the work directory (--project flag or channel mapping)
          2. Loads conversation history from DB
          3. Fetches wiki context
          4. Calls the agent backend
          5. Processes special tags (SCRATCH, DISCOVERY, WIKI, WIKI-PRIVATE, RESEARCH)
          6. Handles handoffs to other agents
          7. Triggers researcher follow-ups
        """
        set_correlation(agent=agent_name, channel=str(channel.id))

        # Resolve project work directory for CLI agents
        if agent_name in ("claude", "codex") and work_dir is None:
            prompt, work_dir = self._resolve_work_dir(prompt, channel)

        # --long flag: use extended timeout (Claude/Codex)
        use_extended_timeout = False
        if agent_name in ("claude", "codex") and re.search(r"--long\b", prompt):
            prompt = re.sub(r"\s*--long\b", "", prompt).strip()
            use_extended_timeout = True

        # Enforce handoff depth limit
        if depth > 0 and depth >= self.MAX_HANDOFF_DEPTH:
            await self._send_as_agent(
                channel,
                source_agent or agent_name,
                f"Handoff chain limit ({self.MAX_HANDOFF_DEPTH}) reached. Stopping.",
            )
            return

        if not self.bot.allowlist.is_allowed(user_id):
            await channel.send(f"You're not authorized to use {agent_name.capitalize()}.")
            return

        agent = self.bot.agents.get(agent_name)
        if not agent:
            await channel.send(f"Unknown agent: {agent_name}")
            return

        agent_config = self.bot.agent_configs.get(agent_name, {})
        handoff_agents = []
        rate_limit_fallback: str | None = None
        placeholder_msg: discord.WebhookMessage | None = None
        last_chunk_edit: float = 0.0
        research_queries: list[str] = []

        lock = self.bot._get_lock(thread_id)
        async with lock:
            if depth == 0 and not origin_already_persisted:
                await self.bot.db.save_message(
                    thread_id, "user", prompt,
                    author_id=str(user_id),
                    message_id=message_id,
                )

            budget = self.bot.conv_config.get("history_budget_chars", 12000)
            history = await self.bot.db.get_history(thread_id, budget)
            if ephemeral_context:
                if not history or history[-1]["role"] != "user":
                    raise RuntimeError(
                        "Expected last history entry to be user message"
                    )
                history[-1]["content"] = ephemeral_context + "\n\n" + history[-1]["content"]

            # Memory injection — shared always; private only for local-inference agents
            _is_local = agent_config.get("inference_backend") == "local"
            memory_block = ""
            if agent_name != "researcher":
                shared_memories = await self.bot.db.get_memories(limit=10)
                if shared_memories:
                    memory_block = _format_memory_block(shared_memories)
                if _is_local and getattr(self.bot, "private_db", None) is not None:
                    private_memories = await self.bot.private_db.get_memories_for_injection(limit=10)
                    if private_memories:
                        private_block = "[Private Memory]\n" + _format_memory_block(private_memories)
                        memory_block = (
                            (memory_block + "\n\n" + private_block)
                            if memory_block
                            else private_block
                        )

            placeholder_msg = await self._start_placeholder(channel, agent_name)

            async def _on_chunk(text: str) -> None:
                """Update the placeholder message with streaming progress (throttled to 1Hz)."""
                nonlocal last_chunk_edit
                if placeholder_msg is None:
                    return
                now = time.monotonic()
                if now - last_chunk_edit < 1.0:
                    return
                last_chunk_edit = now
                preview = text[:1990] + "\u2026" if len(text) > 1990 else text
                try:
                    await placeholder_msg.edit(content=preview)
                except discord.HTTPException:
                    pass

            job_id = await self.bot.db.create_job(thread_id, agent_name, prompt)
            set_correlation(job_id=str(job_id), session_id=thread_id)
            await self.bot.db.update_job(job_id, "running")

            try:
                workspace = await self.bot.db.get_workspace(thread_id, agent_name)
                channel_id_str = str(resolve_channel_id(channel))
                mission = self.bot._get_channel_mission(channel_id_str, agent_name)

                async with channel.typing():
                    # Wiki context — injected for conversational agents (not researcher)
                    wiki_context = ""
                    if (
                        agent_name != "researcher"
                        and getattr(self.bot, "wiki_enabled", False)
                        and getattr(self.bot, "wiki", None) is not None
                    ):
                        try:
                            wiki_context = await self.bot.wiki.get_relevant_context(
                                query=prompt,
                                budget_chars=4000,
                                channel_id=channel_id_str,
                                include_private=_is_local,
                                agent_name=agent_name,
                            ) or ""
                        except Exception:
                            log.warning(
                                "wiki: context lookup failed for agent=%s", agent_name,
                                exc_info=True,
                            )

                    # --- Agent call ---
                    # Hook point: add custom agent routing logic here

                    if agent_name == "local-agent":
                        # Local agent relay path — system prompt is owned by the backend.
                        # Discord context (mission, wiki, memory) is appended to the last user message.
                        relay_messages = [dict(m) for m in history]
                        ctx_block = build_discord_context(
                            self.bot.alert_mention, mission, wiki_context
                        )
                        if memory_block:
                            ctx_block += f"\n\nmemory:\n{memory_block}"
                        if relay_messages and relay_messages[-1]["role"] == "user":
                            relay_messages[-1]["content"] = (
                                relay_messages[-1]["content"] + "\n\n" + ctx_block
                            )
                        if hasattr(agent, "call_streaming"):
                            result = await agent.call_streaming(
                                relay_messages, "", on_chunk=_on_chunk,
                                mission=mission, workspace=workspace,
                            )
                        else:
                            result = await agent.call(
                                relay_messages, "", mission=mission, workspace=workspace
                            )

                    elif agent_name == "researcher":
                        # One-shot query — pass only the current prompt, not history
                        result = await agent.call([{"role": "user", "content": prompt}], "")

                    else:
                        # Cloud agents (Claude, Codex) — system prompt injected here
                        system_prompt = agent_config.get(
                            "system_prompt", f"You are {agent_name.capitalize()}."
                        )
                        if self.bot.alert_mention:
                            system_prompt += (
                                f"\n\nUSER MENTION: To notify the user directly, "
                                f"use {self.bot.alert_mention} in your response."
                            )
                        if wiki_context:
                            system_prompt += f"\n\n## [Wiki Context]\n{wiki_context}"
                        if memory_block:
                            system_prompt += f"\n\n## [Remembered Facts]\n{memory_block}"
                        result = await agent.call(
                            history,
                            system_prompt,
                            mission=mission,
                            workspace=workspace,
                            work_dir=work_dir,
                            timeout=(
                                agent_config.get("timeout_extended")
                                if use_extended_timeout
                                else None
                            ),
                        )

                    if isinstance(result, tuple):
                        response_text, metadata = result
                        if isinstance(metadata, int):
                            metadata = {"tokens_output": metadata or None}
                        elif metadata is None:
                            metadata = {}
                    else:
                        response_text = result
                        metadata = {}

                # --- Process special tags ---

                # SCRATCH — agent working memory, stored per-thread per-agent
                scratch_match = re.search(
                    r"<!--\s*SCRATCH\s*-->(.*?)<!--\s*/SCRATCH\s*-->",
                    response_text,
                    re.DOTALL | re.IGNORECASE,
                )
                if scratch_match:
                    _scratch_raw = scratch_match.group(1).strip()
                    response_text = re.sub(
                        r"\s*<!--\s*SCRATCH\s*-->.*?<!--\s*/SCRATCH\s*-->\s*",
                        "",
                        response_text,
                        flags=re.DOTALL | re.IGNORECASE,
                    ).strip()
                    await self._process_scratch(thread_id, agent_name, _scratch_raw)

                # DISCOVERY — posts to #discoveries channel
                discovery_match = re.search(
                    r"<!--\s*DISCOVERY:\s*(.*?)\s*-->",
                    response_text,
                    re.IGNORECASE,
                )
                if discovery_match:
                    finding = discovery_match.group(1).strip()
                    response_text = re.sub(
                        r"\s*<!--\s*DISCOVERY:.*?-->\s*",
                        "",
                        response_text,
                        flags=re.IGNORECASE,
                    ).strip()
                    await self.bot._post_discovery(finding, agent_name)

                # WIKI — writes to shared wiki
                wiki_blocks = list(re.finditer(
                    r"<!--\s*WIKI:\s*(\S+)\s*-->(.*?)<!--\s*/WIKI\s*-->",
                    response_text,
                    re.DOTALL | re.IGNORECASE,
                ))
                if wiki_blocks:
                    response_text = re.sub(
                        r"\s*<!--\s*WIKI:\s*\S+\s*-->.*?<!--\s*/WIKI\s*-->\s*",
                        "",
                        response_text,
                        flags=re.DOTALL | re.IGNORECASE,
                    ).strip()
                    for wiki_match in wiki_blocks:
                        raw_page_name = wiki_match.group(1).strip()
                        wiki_page_name = raw_page_name.lower()
                        # Validate page name: lowercase alphanumeric + hyphens
                        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*[a-z0-9]", wiki_page_name):
                            log.warning(
                                "wiki: rejected invalid page name from agent: %r", raw_page_name
                            )
                            response_text += (
                                f"\n*[Wiki: rejected invalid page name `{raw_page_name[:40]}`]*"
                            )
                            continue
                        wiki_page_content = wiki_match.group(2).strip()
                        wiki_aliases = []
                        alias_line = re.match(
                            r"^ALIASES:\s*(.+)$", wiki_page_content, re.MULTILINE
                        )
                        if alias_line:
                            wiki_aliases = [a.strip() for a in alias_line.group(1).split(",")]
                            wiki_page_content = wiki_page_content[alias_line.end():].lstrip("\n")
                        try:
                            if (
                                getattr(self.bot, "wiki_enabled", False)
                                and getattr(self.bot, "wiki", None) is not None
                            ):
                                await self.bot.wiki.write_page(
                                    wiki_page_name,
                                    wiki_page_content,
                                    author=agent_name,
                                    source_message_id=None,
                                    source="inline",
                                    aliases=wiki_aliases,
                                )
                                response_text += f"\n*[Wiki: updated `{wiki_page_name}`]*"
                            else:
                                response_text += "\n*[Wiki: write skipped — wiki not configured]*"
                        except Exception as exc:
                            log.error(
                                "Wiki inline write failed for page %s: %s", wiki_page_name, exc
                            )
                            response_text += (
                                f"\n*[Wiki: write failed for `{wiki_page_name}` — {exc}]*"
                            )

                # WIKI-PRIVATE — writes to private wiki tier (local agent only)
                if agent_name == "local-agent":
                    private_wiki_blocks = list(re.finditer(
                        r"<!--\s*WIKI-PRIVATE:\s*(\S+)\s*-->(.*?)<!--\s*/WIKI-PRIVATE\s*-->",
                        response_text,
                        re.DOTALL | re.IGNORECASE,
                    ))
                    if private_wiki_blocks:
                        response_text = re.sub(
                            r"\s*<!--\s*WIKI-PRIVATE:\s*\S+\s*-->.*?<!--\s*/WIKI-PRIVATE\s*-->\s*",
                            "",
                            response_text,
                            flags=re.DOTALL | re.IGNORECASE,
                        ).strip()
                        for pw_match in private_wiki_blocks:
                            raw_page_name = pw_match.group(1).strip()
                            pw_page_name = raw_page_name.lower()
                            if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*[a-z0-9]", pw_page_name):
                                log.warning(
                                    "wiki-private: rejected invalid page name: %r", raw_page_name
                                )
                                response_text += (
                                    f"\n*[Private wiki: rejected invalid page name "
                                    f"`{raw_page_name[:40]}`]*"
                                )
                                continue
                            pw_content = pw_match.group(2).strip()
                            pw_aliases: list[str] = []
                            alias_line = re.match(
                                r"^ALIASES:\s*(.+)$", pw_content, re.MULTILINE
                            )
                            if alias_line:
                                pw_aliases = [a.strip() for a in alias_line.group(1).split(",")]
                                pw_content = pw_content[alias_line.end():].lstrip("\n")
                            try:
                                if (
                                    getattr(self.bot, "wiki_enabled", False)
                                    and getattr(self.bot, "wiki", None) is not None
                                ):
                                    await self.bot.wiki.write_private_draft(
                                        pw_page_name,
                                        pw_content,
                                        author="local-agent",
                                        aliases=pw_aliases,
                                    )
                                    response_text += (
                                        f"\n*[Private wiki: `{pw_page_name}` saved as draft — "
                                        f"use `/wiki-private promote page:{pw_page_name}` to publish]*"
                                    )
                                else:
                                    response_text += (
                                        "\n*[Private wiki: write skipped — wiki not configured]*"
                                    )
                            except Exception as exc:
                                log.error(
                                    "Wiki-private draft write failed for page %s: %s",
                                    pw_page_name, exc,
                                )
                                response_text += (
                                    f"\n*[Private wiki: write failed for `{pw_page_name}` — {exc}]*"
                                )

                # RESEARCH — triggers researcher agent follow-up
                if agent_name != "researcher":
                    research_blocks = list(re.finditer(
                        r"<!--\s*RESEARCH:\s*(.*?)\s*-->",
                        response_text,
                        re.IGNORECASE,
                    ))
                    if research_blocks:
                        response_text = re.sub(
                            r"\s*<!--\s*RESEARCH:\s*.*?-->\s*",
                            "",
                            response_text,
                            flags=re.IGNORECASE,
                        ).strip()
                        research_queries = [
                            m.group(1).strip() for m in research_blocks
                            if m.group(1).strip()
                        ]

                # Scan for leaked secrets before posting
                response_text = scan_output(response_text)

                # Extract handoff commands from the response
                handoff_agents, clean_response = self._extract_handoffs(response_text, agent_name)

                await self.bot.db.save_message(thread_id, "assistant", clean_response)
                await self._finish_with_placeholder(
                    channel, agent_name, placeholder_msg, clean_response
                )
                placeholder_msg = None

                if handoff_agents:
                    targets = ", ".join(t.capitalize() for t, _ in handoff_agents)
                    await channel.send(f"*{agent_name.capitalize()} → {targets}*")

                await self.bot.db.update_job(
                    job_id,
                    "completed",
                    tokens_input=metadata.get("tokens_input"),
                    tokens_output=metadata.get("tokens_output"),
                    tokens_cache_read=metadata.get("tokens_cache_read"),
                    cost_usd=metadata.get("cost_usd"),
                )

                # Context window warning for local agents
                if agent_name == "local-agent":
                    ctx_window = self.bot.agent_configs.get("local-agent", {}).get("context_window", 32768)
                    prompt_tokens = metadata.get("tokens_input") or 0
                    if prompt_tokens and (prompt_tokens / ctx_window) > 0.85:
                        await self.bot._post_to_alerts(
                            f"Local agent context at {prompt_tokens / ctx_window:.1%} "
                            f"({prompt_tokens:,}/{ctx_window:,} tokens) — approaching limit"
                        )

                await self.bot.db.audit(
                    f"{agent_name}_response",
                    f"thread={thread_id} chars={len(clean_response)}",
                )

            except AgentRateLimitError as e:
                await self.bot.db.update_job(job_id, "failed")
                log.warning("%s rate/usage limit hit: %s", agent_name, e)
                # Fallback chain: claude → codex → local-agent; codex → local-agent
                fallback_chain = {"claude": ["codex", "local-agent"], "codex": ["local-agent"]}
                for fallback in fallback_chain.get(agent_name, []):
                    if self.bot._agent_status.get(fallback, True):
                        rate_limit_fallback = fallback
                        break
                if rate_limit_fallback:
                    await self.bot._post_to_alerts(
                        f"{agent_name.capitalize()} usage/rate limit hit — "
                        f"falling back to {rate_limit_fallback.capitalize()}."
                    )
                    switch_msg = (
                        f"*{agent_name.capitalize()} limit reached — "
                        f"switching to {rate_limit_fallback.capitalize()}...*"
                    )
                    if placeholder_msg:
                        try:
                            await placeholder_msg.edit(content=switch_msg)
                            placeholder_msg = None
                        except discord.HTTPException:
                            await channel.send(switch_msg)
                    else:
                        await channel.send(switch_msg)
                else:
                    err_msg = (
                        f"{agent_name.capitalize()} hit its usage limit "
                        "and no fallback is available."
                    )
                    if placeholder_msg:
                        try:
                            await placeholder_msg.edit(content=err_msg)
                            placeholder_msg = None
                        except discord.HTTPException:
                            await channel.send(err_msg)
                    else:
                        await channel.send(err_msg)

            except AgentOfflineError as e:
                await self.bot.db.update_job(job_id, "failed")
                msg = f"{agent_name.capitalize()} is offline: {e}"
                log.error(msg)
                if placeholder_msg:
                    try:
                        await placeholder_msg.edit(content=msg)
                    except discord.HTTPException:
                        await channel.send(msg)
                else:
                    await channel.send(msg)
                return

            except AgentTimeoutError as e:
                await self.bot.db.update_job(job_id, "failed")
                msg = f"{agent_name.capitalize()} timed out: {e}"
                log.error(msg)
                if placeholder_msg:
                    try:
                        await placeholder_msg.edit(content=msg)
                    except discord.HTTPException:
                        await channel.send(msg)
                else:
                    await channel.send(msg)
                return

        # Rate-limit fallback: retry with the fallback agent
        if rate_limit_fallback:
            await self.handle_agent_request(
                agent_name=rate_limit_fallback,
                prompt=prompt,
                thread_id=thread_id,
                channel=channel,
                user_id=user_id,
                depth=depth,
                source_agent=agent_name,
                work_dir=work_dir,
            )
            return

        # Process handoffs
        for target_agent, handoff_prompt in handoff_agents:
            channel_id = resolve_channel_id(channel)
            target_chs = self.bot.agent_channels.get(target_agent, set())
            if should_respond(channel_id, target_chs):
                log.info(
                    "Handoff: %s → %s (depth %d)", agent_name, target_agent, depth + 1
                )
                if self.bot.handoffs_channel_id:
                    handoffs_channel = self.bot.get_channel(self.bot.handoffs_channel_id)
                    if handoffs_channel:
                        preview = (
                            handoff_prompt[:200] + "..."
                            if len(handoff_prompt) > 200
                            else handoff_prompt
                        )
                        await handoffs_channel.send(
                            f"**{agent_name.capitalize()} → {target_agent.capitalize()}**"
                            f" (depth {depth + 1})\n> {preview}"
                        )
                await self.handle_agent_request(
                    agent_name=target_agent,
                    prompt=handoff_prompt,
                    thread_id=thread_id,
                    channel=channel,
                    user_id=user_id,
                    depth=depth + 1,
                    source_agent=agent_name,
                )

        # Trigger researcher agent for any RESEARCH tags
        for query in research_queries:
            await self._handle_research(channel, query, agent_name)

    def _extract_handoffs(
        self,
        response: str,
        source_agent: str,
    ) -> tuple[list[tuple[str, str]], str]:
        """Extract handoff commands from an agent's response.

        Supports two formats:
          @Claude <prompt>   — @mention format (preferred, per system prompt)
          !c <prompt>        — legacy !bang format
        """
        handoffs = []
        lines = response.split("\n")
        clean_lines = []

        _at_mention_re = re.compile(
            r"^@(local-agent|claude|codex)\b\s*(.*)", re.IGNORECASE
        )

        for line in lines:
            stripped = line.strip()
            match = self._HANDOFF_RE.match(stripped)
            if match:
                at_match = _at_mention_re.match(stripped)
                if at_match:
                    agent_name = at_match.group(1).lower()
                    prompt = at_match.group(2).strip()
                    if agent_name != source_agent and prompt:
                        handoffs.append((agent_name, prompt))
                        continue
                    clean_lines.append(line)
                    continue
                # Legacy !bang format
                agents, prompt = parse_commands(stripped)
                agents = [agent for agent in agents if agent != source_agent]
                if agents and prompt:
                    for agent in agents:
                        handoffs.append((agent, prompt))
                    continue
            clean_lines.append(line)

        cleaned = "\n".join(clean_lines).strip()
        return handoffs, cleaned

    async def _handle_research(
        self, channel, query: str, requesting_agent: str
    ) -> None:
        """Call the researcher agent for a RESEARCH tag query and post the result."""
        researcher = self.bot.agents.get("researcher")
        if not researcher:
            log.warning(
                "RESEARCH tag from %s but no researcher agent configured", requesting_agent
            )
            return
        log.info("Researcher query from %s: %r", requesting_agent, query[:100])
        placeholder = await self._start_placeholder(channel, "researcher")
        try:
            raw_result, _ = await researcher.call(
                [{"role": "user", "content": query}], ""
            )
            response = f"**Research:** {scan_output(query)}\n\n{raw_result}"
        except Exception as e:
            log.warning("Researcher call failed for query %r: %s", query, e)
            response = f"*Research failed for '{query[:100]}': {e}*"
        await self._finish_with_placeholder(channel, "researcher", placeholder, response)

    async def _process_scratch(
        self, thread_id: str, agent_name: str, scratch_raw: str
    ):
        """Validate and store agent scratch zone content.

        Scratch is a small JSON object agents use for working memory across turns.
        It is validated strictly: only allowed keys, no instruction-like phrases,
        max 800 chars.
        """
        allowed_keys = {"files_touched", "decisions", "next_step"}
        instruction_phrases = ("always", "never", "from now on")

        try:
            scratch_data = json.loads(scratch_raw)
        except json.JSONDecodeError:
            log.warning(
                "Scratch zone invalid JSON — discarding (thread=%s, agent=%s)",
                thread_id, agent_name,
            )
            return

        if not isinstance(scratch_data, dict):
            log.warning("Scratch zone not a JSON object — discarding")
            return

        extra_keys = set(scratch_data.keys()) - allowed_keys
        if extra_keys:
            log.warning("Scratch zone has disallowed keys %s — discarding", extra_keys)
            return

        scratch_str = json.dumps(scratch_data)
        if any(phrase in scratch_str.lower() for phrase in instruction_phrases):
            log.warning(
                "Scratch zone contains instruction-like content — discarding "
                "(thread=%s, agent=%s)", thread_id, agent_name,
            )
            return

        if len(scratch_str) > 800:
            log.warning(
                "Scratch zone exceeds 800 chars (%d) — discarding", len(scratch_str)
            )
            return

        await self.bot.db.upsert_workspace(thread_id, agent_name, scratch_str)
        log.info(
            "Scratch stored (thread=%s, agent=%s, chars=%d)",
            thread_id, agent_name, len(scratch_str),
        )

    async def _start_placeholder(
        self, channel, agent_name: str
    ) -> "discord.WebhookMessage | None":
        """Send a 'thinking...' placeholder via webhook. Returns the message for later editing."""
        try:
            webhook = await self._get_webhook(channel, agent_name)
            display_name = self.bot.agent_configs.get(agent_name, {}).get(
                "display_name", agent_name.capitalize()
            )
            avatar_url = self.bot.agent_configs.get(agent_name, {}).get("avatar_url") or None
            return await webhook.send(
                content="*thinking...*",
                username=display_name,
                avatar_url=avatar_url,
                wait=True,
            )
        except Exception:
            log.debug(
                "Could not send placeholder for %s — will send response normally", agent_name
            )
            return None

    async def _finish_with_placeholder(
        self,
        channel,
        agent_name: str,
        placeholder_msg: "discord.WebhookMessage | None",
        text: str,
    ):
        """Send final response, editing the placeholder if available."""
        chunks = list(chunk_message(text))
        if not chunks:
            return

        webhook = await self._get_webhook(channel, agent_name)
        display_name = self.bot.agent_configs.get(agent_name, {}).get(
            "display_name", agent_name.capitalize()
        )
        avatar_url = self.bot.agent_configs.get(agent_name, {}).get("avatar_url") or None

        first_chunk, *rest_chunks = chunks

        if placeholder_msg is not None:
            try:
                await placeholder_msg.edit(content=first_chunk)
            except discord.HTTPException:
                await webhook.send(
                    content=first_chunk, username=display_name, avatar_url=avatar_url
                )
        else:
            await webhook.send(
                content=first_chunk, username=display_name, avatar_url=avatar_url
            )

        for chunk in rest_chunks:
            await webhook.send(content=chunk, username=display_name, avatar_url=avatar_url)

    async def _get_webhook(self, channel, agent_name: str) -> discord.Webhook:
        """Get or create a webhook for an agent in a channel.

        Webhooks allow the bot to post messages with custom names and avatars
        so each agent appears as a distinct Discord user.
        """
        key = (channel.id, agent_name)
        if key in self.bot._webhooks:
            return self.bot._webhooks[key]

        display_name = self.bot.agent_configs.get(agent_name, {}).get(
            "display_name", agent_name.capitalize()
        )
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            if webhook.name == display_name:
                self.bot._webhooks[key] = webhook
                return webhook

        webhook = await channel.create_webhook(name=display_name)
        self.bot._webhooks[key] = webhook
        log.info("Created %s webhook in #%s", display_name, channel.name)
        return webhook

    async def _send_as_agent(self, channel, agent_name: str, text: str):
        """Send a message as an agent via webhook."""
        webhook = await self._get_webhook(channel, agent_name)
        display_name = self.bot.agent_configs.get(agent_name, {}).get(
            "display_name", agent_name.capitalize()
        )
        avatar_url = self.bot.agent_configs.get(agent_name, {}).get("avatar_url") or None

        for chunk in chunk_message(text):
            await webhook.send(
                content=chunk,
                username=display_name,
                avatar_url=avatar_url,
            )


async def setup(bot):
    cog = Agents(bot)
    await bot.add_cog(cog)
    # Expose handle_agent_request on the bot for use by other cogs and slash commands
    bot.handle_agent_request = cog.handle_agent_request
    bot._send_as_agent = cog._send_as_agent
