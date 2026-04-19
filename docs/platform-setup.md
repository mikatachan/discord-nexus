# Platform Setup

Full setup instructions for Windows and Mac/Linux.

---

## Prerequisites

- Python 3.11 or higher
- Node.js 18+ (for Claude Code CLI and/or Codex CLI)
- Git
- A Discord application with a bot token

---

## Step 1: Discord Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** — name it whatever you like
3. Go to **Bot** → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent** (if you use the allowlist feature)
5. Copy the **Token** — you'll put it in `.env`

### Invite URL

In **OAuth2 → URL Generator**, select:
- Scopes: `bot`, `applications.commands`
- Permissions: `Send Messages`, `Manage Webhooks`, `Read Message History`, `Embed Links`, `Add Reactions`

Open the generated URL and add the bot to your server.

### Get IDs

Enable **Developer Mode** in Discord (User Settings → Advanced → Developer Mode).
Right-click any server, channel, or user to copy its ID.

---

## Step 2: Clone and Install

```bash
git clone https://github.com/your-org/discord-nexus.git
cd discord-nexus
python -m venv .venv
```

**Windows:**
```cmd
.venv\Scripts\activate
```

**Mac/Linux:**
```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

---

## Step 3: Configure

```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

### .env

```
DISCORD_TOKEN=your_bot_token_here
# LMSTUDIO_API_KEY=       # optional, leave blank for LM Studio / Ollama
# OPENCLAW_GATEWAY_TOKEN= # optional
# PRIVATE_DB_PATH=        # optional, absolute path to private SQLite DB file
```

### config.yaml

Key fields to fill in:

```yaml
bot:
  name: "YourBot"           # Display name in status commands
  allowed_users:
    - YOUR_DISCORD_USER_ID  # Right-click your name → Copy User ID (Dev Mode must be on)

agent_roles:
  claude: YOUR_CLAUDE_ROLE_ID
  codex: YOUR_CODEX_ROLE_ID
  local-agent: YOUR_LOCAL_AGENT_ROLE_ID

discoveries_channel: YOUR_CHANNEL_ID   # Where <!-- DISCOVERY: --> tags are posted
```

Create Discord roles for each agent you want (e.g., "Claude", "Local Agent") and put the role IDs here.
Users mentioning `@Claude` will trigger the Claude agent.

---

## Step 4: Install CLI Agents (Optional)

### Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

### Codex CLI

```bash
npm install -g @openai/codex
# Add OPENAI_API_KEY to .env
```

### Local LLM (LM Studio)

1. Download [LM Studio](https://lmstudio.ai/)
2. Download a model in the Discover tab
3. Go to **Local Server** → click **Start Server**
4. Default port is 1234

### Local LLM (Ollama)

```bash
# Mac/Linux:
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3
ollama serve

# Windows: download installer from ollama.com
```

Update `config.yaml` with your chosen model name.

---

## Step 5: Run

```bash
python bot.py
```

On first run, the bot will:
- Create the `data/` directory
- Create `nexus.db`
- Harden the private DB path (if set) with restricted permissions
- Sync slash commands with Discord (may take up to an hour to propagate globally)

---

## Persistent Operation with PM2

PM2 keeps the bot running and restarts it on crashes or system reboots.

### Install PM2

```bash
npm install -g pm2
```

### Windows

```bash
npm install -g pm2-windows-startup
pm2-windows-startup install
```

Start the bot:
```bash
pm2 start ecosystem.config.js
pm2 save
```

### Mac/Linux

```bash
pm2 start ecosystem.config.js
pm2 startup        # follow the printed instructions
pm2 save
```

### PM2 Commands

```bash
pm2 status                  # check if bot is running
pm2 logs discord-nexus      # view live logs
pm2 restart discord-nexus   # restart
pm2 stop discord-nexus      # stop
```

The bot also supports `/restart` from Discord (allowlisted users only), which calls `sys.exit(0)`
and relies on PM2 to relaunch it automatically.

---

## Slash Command Sync

Slash commands are registered globally on startup. Discord can take up to 1 hour to propagate them.

To force a guild-only sync (instant, for testing):

Add your server ID to `config.yaml`:
```yaml
bot:
  dev_guild_id: YOUR_SERVER_ID  # instant slash command sync for this guild
```

Then in `bot.py`, add to the `on_ready` handler:
```python
guild = discord.Object(id=self.config["bot"]["dev_guild_id"])
self.tree.copy_global_to(guild=guild)
await self.tree.sync(guild=guild)
```

---

## Private Wiki / DB Setup

Private wiki pages are stored in `wiki/private/` inside the repo tree, but gitignored — they
never leave your machine. No setup is required for this; the bot creates the directory on first run.

`PRIVATE_DB_PATH` controls where the *private SQLite database* is stored (metadata, sessions).
This is optional — if unset, the private DB is created alongside the main `nexus.db`.

To store the private DB at a custom location (e.g., outside any synced folder):

```
# .env
PRIVATE_DB_PATH=/absolute/path/to/nexus-private.db
```

On Windows, the bot applies `icacls` to restrict this file to the current user on first run.

---

## Troubleshooting

### Bot is online but not responding

- Check that **Message Content Intent** is enabled in the Discord Developer Portal
- Verify the channel ID is in `agent_channels` in `config.yaml`
- Check that the bot has permission to read messages in that channel

### Slash commands not appearing

- Wait up to 1 hour for global propagation, or use `dev_guild_id` for instant sync
- Make sure `applications.commands` scope was included in the invite URL

### Agent is offline

- Run `/monitor` in Discord to check agent health
- Check the bot logs: `pm2 logs discord-nexus` or `nexus.log`
- For CLI agents: verify `claude --version` or `codex --version` works in the same environment
- For local LLM: verify the server is running and the model is loaded

### Windows: console window appears when agents run

This should not happen in normal operation. If it does, verify `bot.py` is loading agents
with the `_NO_WINDOW` flag (set in `agents/cli.py`). This flag is only applied on `sys.platform == "win32"`.

### Rate limit fallback not working

The fallback chain requires multiple agents to be configured and online.
Check `/monitor` to see which agents are available.
