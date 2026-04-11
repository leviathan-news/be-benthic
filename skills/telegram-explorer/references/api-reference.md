# Telegram Client API Reference

Complete reference for all subcommands of `telegram_client.py`.

## Global Behavior

- All output is JSON to stdout
- Errors are JSON to stderr with non-zero exit code
- Error format: `{"error": "message", "type": "ExceptionType"}`
- Credentials loaded from `~/.claude/telegram-creds.json`
- Session reused from `~/.claude/z_session.session`

---

## dialogs

List recent dialogs (chats, channels, groups, DMs).

**Usage:**
```bash
telegram_client.py dialogs [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `--limit` | int | 20 | Maximum dialogs to return |

**Output schema:**
```json
[
  {
    "id": 123456789,
    "name": "Chat Name",
    "type": "Channel",
    "unread_count": 5,
    "last_message_date": "2026-02-26T10:30:00+00:00",
    "pinned": false
  }
]
```

**Types:** `User`, `Chat`, `Channel`

---

## topics

List forum topics in a supergroup that has topics enabled.

**Usage:**
```bash
telegram_client.py topics <chat> [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier (must be a forum-enabled supergroup) |
| `--limit` | int | 50 | Maximum topics to return |

**Output schema:**
```json
[
  {
    "id": 42,
    "title": "General",
    "date": "2026-02-26T10:30:00+00:00",
    "icon_emoji": "💬"
  }
]
```

---

## messages

Fetch messages from a specific chat.

**Usage:**
```bash
telegram_client.py messages <chat> [--limit N] [--search "query"] [--min-id X] [--max-id Y] [--topic ID]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 20 | Maximum messages to return |
| `--search` | str | None | Filter by text content |
| `--min-id` | int | None | Only messages with ID > this |
| `--max-id` | int | None | Only messages with ID < this |
| `--topic` | int | None | Forum topic ID (for supergroups with topics) |

**Output schema:**
```json
[
  {
    "id": 42,
    "date": "2026-02-26T10:30:00+00:00",
    "sender_id": 123456789,
    "text": "Hello world",
    "reply_to_msg_id": null,
    "media_type": null,
    "forward": null,
    "reactions": [{"emoji": "👍", "count": 3}],
    "views": 150,
    "edit_date": null
  }
]
```

**Media types:** `MessageMediaPhoto`, `MessageMediaDocument`, `MessageMediaWebPage`, `MessageMediaGeo`, `MessageMediaContact`, `MessageMediaPoll`

---

## send

Send a text message to a chat.

**Usage:**
```bash
telegram_client.py send <chat> --text "message" [--topic ID]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--text` | str | required | Message content |
| `--topic` | int | None | Forum topic ID (for supergroups with topics) |

**Output:** Single message object (same schema as `messages`).

---

## reply

Reply to a specific message.

**Usage:**
```bash
telegram_client.py reply <chat> --message-id ID --text "reply text"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Target message ID |
| `--text` | str | required | Reply content |

**Output:** Single message object.

---

## forward

Forward a message from one chat to another.

**Usage:**
```bash
telegram_client.py forward <from_chat> <to_chat> --message-id ID
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `from_chat` | str | required | Source chat |
| `to_chat` | str | required | Destination chat |
| `--message-id` | int | required | Message to forward |

**Output:** Single message object (the forwarded copy).

---

## edit

Edit an existing message.

**Usage:**
```bash
telegram_client.py edit <chat> --message-id ID --text "new text"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message to edit |
| `--text` | str | required | New message content |

**Output:** Single message object (edited).

**Note:** Only own messages can be edited (unless admin in a channel).

---

## delete

Delete one or more messages.

**Usage:**
```bash
telegram_client.py delete <chat> --message-ids "1,2,3"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-ids` | str | required | Comma-separated message IDs |

**Output:**
```json
{
  "deleted_ids": [1, 2, 3],
  "affected_messages": 3
}
```

**Note:** Only own messages can be deleted (unless admin).

---

## react

Send a reaction to a message.

**Usage:**
```bash
telegram_client.py react <chat> --message-id ID --emoji "👍"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Target message |
| `--emoji` | str | required | Reaction emoji |

**Output:**
```json
{
  "ok": true,
  "chat": "@channelname",
  "message_id": 42,
  "emoji": "👍"
}
```

**Common emojis:** 👍 👎 ❤️ 🔥 🎉 😢 😮 🤔 👏 🤯

---

## buttons

Show inline keyboard buttons on a message, with row/col indices for clicking.

**Usage:**
```bash
telegram_client.py buttons <chat> --message-id ID
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message with inline buttons |

**Output schema:**
```json
{
  "message_id": 42,
  "buttons": [
    [
      {"text": "Approve", "row": 0, "col": 0},
      {"text": "Reject", "row": 0, "col": 1}
    ],
    [
      {"text": "Advanced", "row": 1, "col": 0}
    ]
  ]
}
```

---

## click

Click an inline keyboard button by row and column index.

**Usage:**
```bash
telegram_client.py click <chat> --message-id ID --row R --col C
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message with inline buttons |
| `--row` | int | required | Button row index (0-based) |
| `--col` | int | required | Button column index (0-based) |

**Output:**
```json
{
  "ok": true,
  "message_id": 42,
  "button": "Approve",
  "row": 0,
  "col": 0
}
```

**Tip:** Run `buttons` first to see available buttons and their indices.

---

## info

Get detailed info about any entity.

**Usage:**
```bash
telegram_client.py info <entity>
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `entity` | str | required | Username, phone, or ID |

**Output for User:**
```json
{
  "id": 123456789,
  "type": "User",
  "first_name": "John",
  "last_name": "Doe",
  "username": "johndoe",
  "phone": "+1234567890",
  "bot": false,
  "verified": false
}
```

**Output for Channel/Chat:**
```json
{
  "id": -1001234567890,
  "type": "Channel",
  "title": "Channel Name",
  "username": "channelname",
  "megagroup": true,
  "participants_count": 5000
}
```

---

## participants

List members of a group or channel.

**Usage:**
```bash
telegram_client.py participants <chat> [--limit N] [--search "query"]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 50 | Maximum participants |
| `--search` | str | None | Filter by name/username |

**Output schema:**
```json
[
  {
    "id": 123456789,
    "first_name": "John",
    "last_name": "Doe",
    "username": "johndoe",
    "bot": false,
    "role": "admin"
  }
]
```

**Roles:** `creator`, `admin`, `member`, `banned`

---

## download

Download media from a message.

**Usage:**
```bash
telegram_client.py download <chat> --message-id ID [--out ./dir/]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message with media |
| `--out` | str | `.` | Output directory |

**Output:**
```json
{
  "ok": true,
  "path": "/tmp/photo_2026-02-26.jpg"
}
```

**Errors:** Exits 1 if message not found or has no media.

---

## upload

Upload and send a file.

**Usage:**
```bash
telegram_client.py upload <chat> --file ./path [--caption "text"] [--voice-note]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--file` | str | required | Local file path |
| `--caption` | str | None | Caption text |
| `--voice-note` | flag | false | Send as voice note (audio files only) |

**Output:** Single message object (with `media_type` set).

**Supported:** Photos (jpg/png), videos (mp4), documents (any file type). Telethon auto-detects the type.

---

## pinned

Get pinned messages from a chat.

**Usage:**
```bash
telegram_client.py pinned <chat> [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 10 | Maximum pinned messages |

**Output:** Array of message objects.

---

## search-global

Search messages across all chats.

**Usage:**
```bash
telegram_client.py search-global --query "text" [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `--query` | str | required | Search text |
| `--limit` | int | 20 | Maximum results |

**Output:** Array of message objects with additional `chat_id` field.

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FloodWaitError` | Too many requests | Wait the specified seconds |
| `ChatAdminRequiredError` | Insufficient permissions | Need admin rights |
| `MessageNotModifiedError` | Edit with same text | Change the text |
| `UserNotParticipantError` | Not in the chat | Join first |
| `ChannelPrivateError` | Private channel | Need invite |
| `PeerIdInvalidError` | Bad entity identifier | Check the ID/username |

## Chat Identifier Formats

| Format | Example | Notes |
|--------|---------|-------|
| Username | `@channelname` or `channelname` | Most common |
| Phone | `+1234567890` | For contacts |
| User ID | `123456789` | Positive integer |
| Chat ID | `-123456789` | Negative for groups |
| Channel ID | `-1001234567890` | Prefix -100 for channels |
| Invite link | `https://t.me/joinchat/ABC` | For private chats |
| t.me link | `https://t.me/channelname` | Public links |
