// PM2 ecosystem config for discord-nexus
//
// Usage:
//   pm2 start ecosystem.config.js
//   pm2 save
//
// Startup (run ONCE after first deploy):
//   Windows:  npm install -g pm2-windows-startup && pm2-startup install
//   Mac/Linux: pm2 startup   (follow the printed command)
//
// After startup is configured: pm2 save
//
// The bot will then survive reboots automatically.

module.exports = {
  apps: [
    {
      name: "discord-nexus",
      script: "bot.py",
      interpreter: "python",  // or "python3" on Mac/Linux if that's your binary name

      // Working directory — set to the repo root
      cwd: "./",

      // Restart on crash, with exponential backoff
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,     // ms between restarts
      exp_backoff_restart_delay: 100,

      // Log file paths (relative to cwd)
      out_file: "./data/logs/pm2-out.log",
      error_file: "./data/logs/pm2-err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",

      // Environment variables
      // DO NOT put real secrets here — use .env instead
      env: {
        DISCORD_TOKEN: "REPLACE_WITH_YOUR_TOKEN_OR_USE_ENV_FILE",
        // LMSTUDIO_API_KEY: "",
        // OPENCLAW_GATEWAY_TOKEN: "",
        // PRIVATE_DB_PATH: "",
      },

      // Watch mode — disabled for production (PM2 restarts on file changes when true)
      watch: false,
    },
  ],
};
