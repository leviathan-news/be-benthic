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
      //   CHANNELS: '["@examplechannel"]',
      //   ID_SOURCES: '{"-1001234567890": "Invite-Only Feed"}',
      //   BENTHIC_NEWS_CHANNEL: "@your_news_channel",  // unset = no channel posts
      // }
    },
    {
      name: "benthic-bot",
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
    {
      name: "benthic-builder",
      cwd: __dirname,
      interpreter: ".venv/bin/python3",
      script: "benthic-builder.py",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // Optional daemon — remove this block if you don't run autonomous builds.
      // env: {
      //   BUILD_GITHUB_ORG: "YourGithubOrg",
      //   BUILD_GIT_USER_NAME: "Agent Builder",
      //   BUILD_GIT_USER_EMAIL: "agent@example.com",
      // }
    },
  ]
};
