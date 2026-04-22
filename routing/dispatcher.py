"""Command parsing, agent routing, and channel resolution for discord-nexus.

Handles !bang command parsing (e.g. !claude, !c, !codex, !all) and resolves
which agent(s) should respond in a given channel.
"""

import logging
import re

log = logging.getLogger(__name__)

# Map agent names to their bang command regex patterns.
# Add aliases here if you want shorthand commands (e.g. !m for local-agent).
BANG_ALIASES = {
    "local-agent": r"!(?:local-agent|m)",
    "claude": r"!(?:claude|c)",
    "codex": r"!(?:codex|g)",
    "researcher": r"!(?:researcher|research|r)",
}

# Agents included in !all/!a broadcasts.
# The researcher is a tool agent and is excluded from broadcasts.
ALL_AGENTS = ["local-agent", "claude", "codex"]

# Pattern to find any bang command (used to strip them from the prompt text)
_ALL_BANGS = re.compile(
    r"!(?:local-agent|m|claude|c|codex|g|researcher|research|r|all|a)\b",
    re.IGNORECASE,
)


def parse_command(content: str) -> tuple[str | None, str | None]:
    """Parse a message for a single agent bang command.

    Convenience wrapper around parse_commands() for slash command handlers.

    Returns:
        (agent_name, prompt) if a command is found, else (None, None).
    """
    agents, prompt = parse_commands(content)
    if agents:
        return (agents[0], prompt)
    return (None, None)


def parse_commands(content: str) -> tuple[list[str], str | None]:
    """Parse a message for one or more agent bang commands.

    Supports:
      !all or !a     → broadcasts to all agents in ALL_AGENTS
      !claude or !c  → routes to claude
      !codex or !g   → routes to codex
      !local-agent or !m  → routes to local-agent (local agent)
      !research or !r → routes to researcher

    Returns:
        (list_of_agent_names, prompt) — agents list is empty if no commands found.
    """
    stripped = content.strip()

    # Check for !all / !a first
    if re.search(r"!(?:all|a)\b", stripped, re.IGNORECASE):
        prompt = _ALL_BANGS.sub("", stripped).strip()
        return (list(ALL_AGENTS), prompt if prompt else None)

    agents = []
    for agent_name, alias_pattern in BANG_ALIASES.items():
        if re.search(alias_pattern + r"\b", stripped, re.IGNORECASE):
            agents.append(agent_name)

    if not agents:
        return ([], None)

    # Strip all bang commands from the message to get the clean prompt
    prompt = _ALL_BANGS.sub("", stripped).strip()
    return (agents, prompt if prompt else None)


# Barrier keywords — a line matching any of these separates sequential stages.
# All synonyms collapse to the same meaning: "wait for the previous stage to finish."
_BARRIER_RE = re.compile(
    r"^\s*(?:THEN|AFTER|NEXT|BEFORE|SEQUENTIAL|WAIT|AFTERWARDS|AFTER\s+THAT|WHEN\s+DONE|ONCE\s+DONE)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def split_stages(content: str) -> list[str]:
    """Split a message into sequential stages on barrier keywords.

    Returns a list of text chunks. Adjacent chunks run sequentially;
    agents within a single chunk run in parallel.
    If no barrier keywords are found, returns a single-element list.
    """
    parts = _BARRIER_RE.split(content)
    return [p.strip() for p in parts if p.strip()]


def _parse_chunk(chunk: str) -> list[tuple[str, str]]:
    """Parse a single chunk (no barrier keywords) into (agent, prompt) pairs."""
    # !all / !a — broadcast, no splitting
    if re.search(r"!(?:all|a)\b", chunk, re.IGNORECASE):
        prompt = _ALL_BANGS.sub("", chunk).strip()
        if prompt:
            return [("__all__", prompt)]
        return []

    hits: list[tuple[int, str, re.Match]] = []
    for agent_name, alias_pattern in BANG_ALIASES.items():
        for m in re.finditer(alias_pattern + r"\b", chunk, re.IGNORECASE):
            hits.append((m.start(), agent_name, m))

    if not hits:
        return []

    hits.sort(key=lambda h: h[0])

    sections: list[tuple[str, str]] = []
    for i, (pos, agent_name, match) in enumerate(hits):
        text_start = match.end()
        text_end = hits[i + 1][0] if i + 1 < len(hits) else len(chunk)
        prompt = chunk[text_start:text_end].strip()
        if prompt:
            sections.append((agent_name, prompt))

    return sections


def parse_sectioned_commands(content: str) -> list[list[tuple[str, str]]]:
    """Parse a message into sequential stages of per-agent (agent_name, prompt) pairs.

    Barrier keywords (THEN, AFTER, NEXT, etc.) separate stages that run sequentially.
    Agents within a single stage run in parallel.

    Returns:
        List of stages, where each stage is a list of (agent_name, prompt) pairs.
        Empty list if no commands found.
    """
    stages = split_stages(content)
    result: list[list[tuple[str, str]]] = []
    for chunk in stages:
        sections = _parse_chunk(chunk)
        if sections:
            result.append(sections)
    return result


def resolve_channel_id(channel) -> int:
    """Resolve thread→parent channel ID for config lookup.

    Discord threads have a parent_id pointing to the parent channel.
    We use the parent channel ID for config matching (active_channels sets).
    """
    parent_id = getattr(channel, "parent_id", None)
    return parent_id or channel.id


def should_respond(channel_id: int, active_channels: set[int]) -> bool:
    """Check if an agent should respond in this channel.

    Args:
        channel_id:      The resolved channel ID (use resolve_channel_id first).
        active_channels: The set of channel IDs this agent is active in.
    """
    return channel_id in active_channels
