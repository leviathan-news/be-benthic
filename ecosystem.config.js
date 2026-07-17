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
      name: "benthic-api",
      cwd: __dirname,
      interpreter: ".venv/bin/python3",
      script: "benthic_api.py",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // env: {
      //   API_KEY: "static-bearer-token-for-your-gateway",
      //   API_PORT: "8099",
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
