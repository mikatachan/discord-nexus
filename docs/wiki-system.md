# Wiki System

## Overview

discord-nexus includes a flat-file Markdown wiki that agents and users can read and write.
It has two storage tiers:

| Tier | Location | Who can write | Who can read |
|---|---|---|---|
| Public | `wiki/pages/` | Any agent, any user | Everyone |
| Private | `wiki/private/` (gitignored) | Agents via WIKI-PRIVATE tag | Local agent only (by default) |

---

## Directory Structure

```
wiki/
├── index.md            # Public page index (Markdown, auto-maintained)
├── pages/              # Public Markdown pages
│   ├── example-page.md
│   └── ...
├── drafts/             # Public draft pages (not yet promoted to pages/)
│   └── ...
└── private/            # Gitignored — never committed to repo
    ├── index.md        # Private page index
    ├── pages/          # Private published pages
    └── drafts/         # Private drafts awaiting promotion
```

Private wiki pages (`wiki/private/`) are gitignored and never committed. They live inside
the repo tree but only on the local machine. `PRIVATE_DB_PATH` (in `.env`) controls the
location of the *private SQLite database* — a separate file for private metadata and
session state. On Windows, that database file is hardened with `icacls` on first run.

---

## Writing to the Wiki

### Via agent tags

Agents write to the wiki using structured HTML comment tags in their response.

**Public page:**
```
<!-- WIKI: page-name
# Page Title

Content goes here. Standard Markdown.
-->
```

**Private page:**
```
<!-- WIKI-PRIVATE: page-name
# Private Content

This will not be committed to the repo.
-->
```

Page names must be lowercase, hyphen-separated. The bot creates or overwrites `wiki/pages/page-name.md`.

### Via slash command

Users can also write pages directly:

```
/wiki write page-name
```

Then paste the content in a follow-up prompt.

---

## Reading from the Wiki

### Via slash command

```
/wiki read page-name       — display a page
/wiki list                 — list all pages
/wiki search <terms>       — keyword search across pages
```

### Auto-injected context

When an agent is invoked, the bot calls `WikiStore.get_relevant_context(query)` to find wiki pages
relevant to the current prompt. These are injected into the agent's system prompt automatically.

This means agents have access to the wiki without being told about it explicitly.
Control how much context is injected via `config.yaml`:

```yaml
wiki:
  max_context_pages: 3
  max_context_chars: 2000
```

---

## Private Wiki

The private wiki stores sensitive content in `wiki/private/` — inside the repo tree but
gitignored, so it never leaves your machine.

### Setup

1. The `wiki/private/` directory is created automatically on first run (no manual setup needed).
2. Optionally, set `PRIVATE_DB_PATH` in `.env` to control where the private SQLite database is stored:

```
PRIVATE_DB_PATH=/path/to/discord-nexus/nexus-private.db
```

If unset, the private DB defaults to `discord-nexus-private.db` in the same directory as the
main database. On Windows, the DB file is hardened with `icacls` on first run (owner-only access).

### Usage

Agents write to the private wiki using the `<!-- WIKI-PRIVATE: -->` tag.

Private pages are only accessible to agents configured with `include_private_wiki: true`
(or agents whose `agent_name` is `"local-agent"` in the default routing setup).

---

## Draft System

Pages can be written to `wiki/drafts/` before being promoted to `wiki/pages/`.
This is used by the auto-ingest loop in `cogs/wiki.py`.

### Promotion

```
/wiki promote page-name    — promote a draft to pages/
/wiki demote page-name     — move a page back to drafts/
```

The curation loop (if enabled) periodically asks the local LLM to review drafts
and suggest which should be promoted. This is a passive background task.

Enable it in `config.yaml`:

```yaml
wiki:
  auto_curate: true
  curate_interval_minutes: 60
```

---

## Ingest Loop

The wiki cog runs an ingest loop that watches for new wiki content from agent output.
When agents write `<!-- WIKI: -->` or `<!-- WIKI-PRIVATE: -->` tags, the content is:

1. Written to the appropriate tier
2. Indexed in `wiki/index.json`
3. Optionally fed into the local LLM for curation scoring

Control the ingest loop:

```yaml
wiki:
  auto_ingest: true
  ingest_interval_minutes: 10
```

---

## Index

`wiki/index.md` is a Markdown file maintained automatically. Each line represents one page:

```
- [page-name](pages/page-name.md) — Page Title | aliases: alias1, alias2
```

`wiki/private/index.md` has the same format for private pages. The index is used by
`get_relevant_context()` to select relevant pages for agent prompts without reading every
file on each request.

---

## Config Reference

```yaml
wiki:
  enabled: true                  # Set to false to disable the wiki system entirely
  path: "wiki"                   # Path to wiki directory (relative to bot root)
  # pinned_pages: []             # Pages always prepended to agent context (must exist at startup)
```
