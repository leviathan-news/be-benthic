module.exports = {
  apps: [
    {
      name: "ln-agent",
      cwd: __dirname,
      interpreter: ".venv/bin/python3",
      script: "ln-agent.py",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // Configure environment variables here or export them before starting.
      // env: {
      //   BOT_HQ_GROUP_ID: "-100...",
      //   WALLET_KEY_FILE: "~/.claude/.ln-wallet-key",
      //   TELEGRAM_CREDS_FILE: "~/.claude/telegram-creds.json",
      // }
    },
    {
      name: "chat-bot",
      cwd: __dirname,
      interpreter: ".venv/bin/python3",
      script: "benthic-bot.py",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // env: {
      //   BOT_TOKEN: "your_telegram_bot_token",
      //   BOT_USERNAME: "my_bot",
      //   AGENTS_GROUP_ID: "-100...",
      // }
    },
  ]
};
