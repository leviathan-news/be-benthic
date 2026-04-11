---
name: telegram-explorer
description: >
  This skill should be used when the user asks about Telegram content, messages, channels, groups,
  or when the user asks to "read Telegram messages", "send a Telegram message", "check Telegram",
  "browse Telegram", "search Telegram", "list Telegram chats", "download from Telegram",
  "upload to Telegram", "forward a message", "react to a message on Telegram",
  "get Telegram group participants", "send this to the chat", "send this file",
  "reply to that message", "check what was said in", "read the latest messages",
  "message the team", "share this in Telegram", "upload this to the group",
  "what's being discussed in", "post this in the dev chat", "send this .md file",
  or mentions Telegram handles, channel names, chat groups, or any task involving
  Telegram interaction, communication, or file sharing.
---

# Telegram Explorer

Browse, read, search, send, and interact with Telegram via Telethon (MTProto Client API).

## Setup

- **Credentials:** `~/.claude/telegram-creds.json` with `api_id` (int) and `api_hash` (string)
- **Session:** `~/.claude/z_session.session` (pre-authenticated Telethon session for interactive use)
  - Note: `ln-agent.py` uses a separate session (`~/.claude/agent_session.session`) for automated runs. These are independent — the skill and agent don't share sessions.
- **Python:** Use the project venv: `.venv/bin/python3`

## How to Use

Execute the client script via Bash for all Telegram operations:

```bash
.venv/bin/python3 skills/telegram-explorer/scripts/telegram_client.py <subcommand> [args]
```

All output is JSON. Parse it to present results in a readable format.

## Available Subcommands

### Browsing & Discovery

| Command | Purpose |
|---------|---------|
| `dialogs --limit N` | List recent chats, channels, groups |
| `topics <chat> --limit N` | List forum topics in a group/channel |
| `info <entity>` | Get details about a user, chat, or channel |
| `participants <chat> --limit N --search "query"` | List group/channel members |

### Reading Messages

| Command | Purpose |
|---------|---------|
| `messages <chat> --limit N` | Fetch recent messages |
| `messages <chat> --search "query"` | Search messages in a chat |
| `messages <chat> --min-id X --max-id Y` | Fetch messages in an ID range |
| `messages <chat> --topic ID --limit N` | Fetch messages from a specific forum topic |
| `pinned <chat>` | Get pinned messages |
| `search-global --query "text" --limit N` | Search across all chats |

### Sending & Interacting

| Command | Purpose |
|---------|---------|
| `send <chat> --text "message"` | Send a new message |
| `send <chat> --text "message" --topic ID` | Send to a specific forum topic |
| `reply <chat> --message-id ID --text "reply"` | Reply to a specific message |
| `forward <from_chat> <to_chat> --message-id ID` | Forward a message |
| `edit <chat> --message-id ID --text "new text"` | Edit own message |
| `delete <chat> --message-ids "1,2,3"` | Delete messages |
| `react <chat> --message-id ID --emoji "👍"` | React to a message |

### Inline Buttons

| Command | Purpose |
|---------|---------|
| `buttons <chat> --message-id ID` | Show inline keyboard buttons with row/col indices |
| `click <chat> --message-id ID --row R --col C` | Click an inline button |

### Media

| Command | Purpose |
|---------|---------|
| `download <chat> --message-id ID --out ./dir/` | Download media from a message |
| `upload <chat> --file ./path --caption "text"` | Upload and send a file |

## Chat Identifiers

The `<chat>` argument accepts multiple formats:
- **Username:** `@channelname` or `channelname`
- **Numeric ID:** `-1001234567890` (channels/supergroups use `-100` prefix)
- **Phone number:** `+1234567890`
- **Invite link:** `https://t.me/joinchat/...`

## Important Notes

- **Rate limits:** Telegram enforces rate limits. Space out bulk operations. If a `FloodWaitError` occurs, the error JSON includes the wait time in seconds.
- **Permissions:** Some operations (delete, edit) only work on own messages unless admin.
- **Message ordering:** Messages are returned newest-first by default.
- **Entity resolution:** First call to a new entity may be slow as Telethon resolves it.

## Additional Resources

- **`references/api-reference.md`** — Complete subcommand reference with all arguments and output schemas
