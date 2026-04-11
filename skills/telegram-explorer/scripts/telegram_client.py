#!/usr/bin/env python3
"""
Telegram client wrapper for Claude Code.

Wraps Telethon into a CLI with JSON output for all operations:
dialogs, messages, send, reply, forward, react, edit, delete,
info, participants, download, upload, search, pinned.

Config: ~/.claude/telegram-creds.json  (api_id, api_hash)
Session: ~/.claude/z_session.session

All output is JSON to stdout. Errors go to stderr with non-zero exit.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# --- Resolve paths for config and session ---
CLAUDE_DIR = Path.home() / ".claude"
CREDS_PATH = CLAUDE_DIR / "telegram-creds.json"
SESSION_PATH = CLAUDE_DIR / "z_session"


def load_creds():
    """
    Load api_id and api_hash from ~/.claude/telegram-creds.json.
    Exits with error if the file is missing or malformed.
    """
    if not CREDS_PATH.exists():
        print(json.dumps({"error": f"Credentials file not found at {CREDS_PATH}"}), file=sys.stderr)
        sys.exit(1)
    with open(CREDS_PATH, "r") as f:
        creds = json.load(f)
    if "api_id" not in creds or "api_hash" not in creds:
        print(json.dumps({"error": "telegram-creds.json must contain 'api_id' and 'api_hash'"}), file=sys.stderr)
        sys.exit(1)
    return creds["api_id"], creds["api_hash"]


def make_client():
    """
    Create and return a TelegramClient using stored creds and session.
    The session file at ~/.claude/z_session.session is reused across calls
    so no re-authentication is needed.
    """
    from telethon import TelegramClient
    api_id, api_hash = load_creds()
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    # Disable default markdown parsing to prevent escaping special chars
    # like ! → \! in sent/edited messages.
    client.parse_mode = None
    return client


async def resolve_entity(client, identifier):
    """
    Resolve a chat identifier to a Telethon entity.
    Handles numeric IDs correctly by detecting the ID range:
      - IDs starting with -100 are channels/supergroups (strip -100 prefix)
      - Negative IDs are legacy group chats
      - Positive IDs are users
    Strings (usernames, phones, links) are passed through to get_entity directly.
    """
    from telethon.tl.types import PeerUser, PeerChat, PeerChannel

    # --- Try to parse as integer for numeric ID handling ---
    try:
        num_id = int(identifier)
    except (ValueError, TypeError):
        # Not numeric — treat as username, phone, or link
        return await client.get_entity(identifier)

    # --- Numeric ID: determine type from sign and prefix ---
    if num_id > 0:
        # Positive = user ID
        return await client.get_entity(PeerUser(num_id))
    elif str(num_id).startswith("-100"):
        # Channel/supergroup: strip the -100 prefix to get the real channel ID
        channel_id = int(str(num_id)[4:])
        return await client.get_entity(PeerChannel(channel_id))
    else:
        # Negative without -100 prefix = legacy group chat
        return await client.get_entity(PeerChat(abs(num_id)))


def serialize_buttons(reply_markup):
    """
    Extract inline keyboard buttons from a message's reply_markup.
    Parses each button's type, text label, and type-specific data:
      - callback: has 'data' field (base64-encoded callback bytes)
      - url: has 'url' field
      - switch_inline: has 'query' field (inline query string)
      - text: plain keyboard button with no extra data
    Returns a flat list of button dicts with row/col indices for click targeting.
    Returns None if no reply_markup or no rows present.
    """
    if not reply_markup or not hasattr(reply_markup, "rows"):
        return None

    buttons = []
    for row_idx, row in enumerate(reply_markup.rows):
        for col_idx, btn in enumerate(row.buttons):
            # --- Base button info: position and visible label ---
            btn_data = {
                "row": row_idx,
                "col": col_idx,
                "text": btn.text,
            }

            # --- Determine button type from Telethon class name ---
            btn_type = type(btn).__name__

            if btn_type == "KeyboardButtonCallback":
                # Callback button: data is bytes, encode as base64 for JSON safety
                btn_data["type"] = "callback"
                btn_data["data"] = base64.b64encode(btn.data).decode("ascii") if btn.data else None
            elif btn_type == "KeyboardButtonUrl":
                # URL button: opens a link when clicked
                btn_data["type"] = "url"
                btn_data["url"] = btn.url
            elif btn_type == "KeyboardButtonSwitchInline":
                # Switch-inline button: triggers inline query in another chat
                btn_data["type"] = "switch_inline"
                btn_data["query"] = btn.query
            elif btn_type == "KeyboardButtonUrlAuth":
                # URL auth button: opens URL with authorization
                btn_data["type"] = "url_auth"
                btn_data["url"] = btn.url
            else:
                # Fallback for any other button type (plain text keyboard, etc.)
                btn_data["type"] = "text"

            buttons.append(btn_data)

    return buttons if buttons else None


def serialize_message(msg):
    """
    Convert a Telethon Message object into a JSON-serializable dict.
    Extracts: id, date, sender_id, text, reply_to, media type, forward info,
    reactions, views, edit_date, and inline keyboard buttons.
    """
    data = {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "sender_id": msg.sender_id,
        "text": msg.raw_text or "",
        "reply_to_msg_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
    }

    # --- Media detection: report the type string if media is attached ---
    if msg.media:
        media_type = type(msg.media).__name__
        data["media_type"] = media_type
    else:
        data["media_type"] = None

    # --- Forward info: who originally sent the forwarded message ---
    if msg.forward:
        data["forward"] = {
            "from_id": getattr(msg.forward, "from_id", None),
            "from_name": getattr(msg.forward, "from_name", None),
            "date": msg.forward.date.isoformat() if msg.forward.date else None,
        }
        # from_id can be a PeerUser/PeerChannel object, extract the raw int
        fid = data["forward"]["from_id"]
        if fid and hasattr(fid, "user_id"):
            data["forward"]["from_id"] = fid.user_id
        elif fid and hasattr(fid, "channel_id"):
            data["forward"]["from_id"] = fid.channel_id
    else:
        data["forward"] = None

    # --- Reactions: list of (emoji, count) pairs if present ---
    if msg.reactions and msg.reactions.results:
        data["reactions"] = [
            {"emoji": str(r.reaction.emoticon) if hasattr(r.reaction, "emoticon") else str(r.reaction), "count": r.count}
            for r in msg.reactions.results
        ]
    else:
        data["reactions"] = []

    data["views"] = msg.views
    data["edit_date"] = msg.edit_date.isoformat() if msg.edit_date else None

    # --- Inline keyboard buttons: extract from reply_markup if present ---
    data["buttons"] = serialize_buttons(msg.reply_markup)

    return data


def serialize_dialog(dialog):
    """
    Convert a Telethon Dialog object into a JSON-serializable dict.
    Includes: entity id, name, type (User/Chat/Channel), unread count,
    last message date, and pinned status.
    """
    entity = dialog.entity
    entity_type = type(entity).__name__  # User, Chat, Channel, etc.

    return {
        "id": dialog.id,
        "name": dialog.name,
        "type": entity_type,
        "unread_count": dialog.unread_count,
        "last_message_date": dialog.date.isoformat() if dialog.date else None,
        "pinned": dialog.pinned,
    }


def serialize_entity(entity):
    """
    Convert a Telethon entity (User, Chat, Channel) into a JSON-serializable dict.
    Handles User fields (first_name, last_name, username, phone, bot, verified)
    and Chat/Channel fields (title, username, megagroup, participants_count).
    """
    data = {
        "id": entity.id,
        "type": type(entity).__name__,
    }
    # --- User-specific fields ---
    if hasattr(entity, "first_name"):
        data["first_name"] = entity.first_name
        data["last_name"] = entity.last_name
        data["username"] = entity.username
        data["phone"] = entity.phone
        data["bot"] = getattr(entity, "bot", False)
        data["verified"] = getattr(entity, "verified", False)
    # --- Chat/Channel-specific fields ---
    if hasattr(entity, "title"):
        data["title"] = entity.title
        data["username"] = getattr(entity, "username", None)
        data["megagroup"] = getattr(entity, "megagroup", False)
        data["participants_count"] = getattr(entity, "participants_count", None)
    return data


def serialize_participant(p):
    """
    Convert a Telethon participant (ChatParticipant or ChannelParticipant)
    into a JSON-serializable dict. Extracts user id, name, username, and role
    (admin/creator/banned/member).
    """
    data = {
        "id": p.id,
        "first_name": getattr(p, "first_name", None),
        "last_name": getattr(p, "last_name", None),
        "username": getattr(p, "username", None),
        "bot": getattr(p, "bot", False),
    }
    # --- Determine participant role from the Telethon type name ---
    ptype = type(p.participant).__name__ if hasattr(p, "participant") else ""
    if "Admin" in ptype:
        data["role"] = "admin"
    elif "Creator" in ptype:
        data["role"] = "creator"
    elif "Banned" in ptype:
        data["role"] = "banned"
    else:
        data["role"] = "member"
    return data


# ============================================================
# Subcommand handlers — each is an async function that receives
# the parsed args, creates a client, does the work, and prints
# JSON output.
# ============================================================


async def cmd_dialogs(args):
    """
    List recent dialogs (chats, channels, groups, DMs).
    --limit controls how many to fetch (default 20).
    Output: JSON array of dialog objects.
    """
    client = make_client()
    async with client:
        dialogs = await client.get_dialogs(limit=args.limit)
        result = [serialize_dialog(d) for d in dialogs]
        print(json.dumps(result, indent=2, ensure_ascii=False))


async def cmd_topics(args):
    """
    List forum topics in a group/channel that has topics enabled.
    Scans recent messages for MessageActionTopicCreate service messages
    to discover topic IDs and titles (Telethon 1.x lacks GetForumTopicsRequest).
    --limit controls how many messages to scan (default 1000, higher = more topics found).
    Output: JSON array of {topic_id, title} objects.
    """
    from telethon.tl.types import MessageActionTopicCreate

    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        topics = []
        seen = set()
        # --- Scan messages for topic-creation service messages ---
        async for msg in client.iter_messages(entity, limit=args.limit):
            if msg.action and isinstance(msg.action, MessageActionTopicCreate):
                if msg.id not in seen:
                    seen.add(msg.id)
                    topics.append({"topic_id": msg.id, "title": msg.action.title})
            # --- Also collect topic IDs from reply_to fields of regular messages ---
            elif msg.reply_to and hasattr(msg.reply_to, "forum_topic") and msg.reply_to.forum_topic:
                tid = msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id
                if tid and tid not in seen:
                    seen.add(tid)
                    # Try to fetch the topic-creation message to get the title
                    try:
                        topic_msg = await client.get_messages(entity, ids=tid)
                        if topic_msg and topic_msg.action and hasattr(topic_msg.action, "title"):
                            topics.append({"topic_id": tid, "title": topic_msg.action.title})
                        else:
                            topics.append({"topic_id": tid, "title": None})
                    except Exception:
                        topics.append({"topic_id": tid, "title": None})
        # Sort by topic_id ascending for stable output
        topics.sort(key=lambda t: t["topic_id"])
        print(json.dumps(topics, indent=2, ensure_ascii=False))


async def cmd_messages(args):
    """
    Fetch recent messages from a chat/channel/group.
    <chat> can be a username, phone, numeric ID, or invite link.
    --limit controls count (default 20).
    --search filters messages by text content.
    --min-id / --max-id constrain the message ID range.
    --topic restricts to a specific forum topic by its ID.
    Output: JSON array of message objects (newest first).
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        kwargs = {"entity": entity, "limit": args.limit}
        if args.search:
            kwargs["search"] = args.search
        if args.min_id:
            kwargs["min_id"] = args.min_id
        if args.max_id:
            kwargs["max_id"] = args.max_id
        if args.topic:
            # reply_to filters messages within a specific forum topic thread
            kwargs["reply_to"] = args.topic
        messages = await client.get_messages(**kwargs)
        result = [serialize_message(m) for m in messages]
        print(json.dumps(result, indent=2, ensure_ascii=False))


async def cmd_send(args):
    """
    Send a new text message to a chat.
    --topic sends to a specific forum topic by its ID.
    Output: JSON object of the sent message.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        kwargs = {"entity": entity, "message": args.text, "parse_mode": None}
        if args.topic:
            # reply_to=topic_id sends the message into that forum topic thread
            kwargs["reply_to"] = args.topic
        msg = await client.send_message(**kwargs)
        print(json.dumps(serialize_message(msg), indent=2, ensure_ascii=False))


async def cmd_reply(args):
    """
    Reply to a specific message by its ID.
    --message-id is the target message, --text is the reply body.
    Output: JSON object of the sent reply message.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        msg = await client.send_message(entity, args.text, reply_to=args.message_id, parse_mode=None)
        print(json.dumps(serialize_message(msg), indent=2, ensure_ascii=False))


async def cmd_forward(args):
    """
    Forward a message from one chat to another.
    --message-id is the message to forward in <from_chat>.
    <to_chat> is the destination.
    Output: JSON object of the forwarded message.
    """
    client = make_client()
    async with client:
        from_entity = await resolve_entity(client, args.from_chat)
        to_entity = await resolve_entity(client, args.to_chat)
        result = await client.forward_messages(to_entity, args.message_id, from_entity)
        # forward_messages returns a list when given a single ID; unwrap it
        if isinstance(result, list):
            result = result[0]
        print(json.dumps(serialize_message(result), indent=2, ensure_ascii=False))


async def cmd_edit(args):
    """
    Edit an existing message sent by the authenticated user.
    --message-id targets the message, --text is the new content.
    Output: JSON object of the edited message.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        msg = await client.edit_message(entity, args.message_id, args.text, parse_mode=None)
        print(json.dumps(serialize_message(msg), indent=2, ensure_ascii=False))


async def cmd_delete(args):
    """
    Delete one or more messages by ID from a chat.
    --message-ids is a comma-separated list of IDs.
    Output: JSON object with list of deleted IDs and success status.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        ids = [int(x.strip()) for x in args.message_ids.split(",")]
        result = await client.delete_messages(entity, ids)
        print(json.dumps({"deleted_ids": ids, "affected_messages": getattr(result, "pts_count", len(ids))}, indent=2))


async def cmd_react(args):
    """
    Send a reaction emoji to a specific message.
    --message-id targets the message, --emoji is the reaction (e.g. "👍").
    Output: confirmation JSON with chat, message_id, and emoji.
    """
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        await client(SendReactionRequest(
            peer=entity,
            msg_id=args.message_id,
            reaction=[ReactionEmoji(emoticon=args.emoji)]
        ))
        print(json.dumps({"ok": True, "chat": str(args.chat), "message_id": args.message_id, "emoji": args.emoji}, indent=2))


async def cmd_info(args):
    """
    Get detailed info about a Telegram entity (user, chat, or channel).
    <entity> can be a username, phone number, or numeric ID.
    Output: JSON object with entity details.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.entity)
        print(json.dumps(serialize_entity(entity), indent=2, ensure_ascii=False))


async def cmd_participants(args):
    """
    List participants of a group or channel.
    --limit caps the number returned (default 50).
    --search filters by name/username.
    Output: JSON array of participant objects.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        kwargs = {"entity": entity, "limit": args.limit}
        if args.search:
            kwargs["search"] = args.search
        participants = await client.get_participants(**kwargs)
        result = [serialize_participant(p) for p in participants]
        print(json.dumps(result, indent=2, ensure_ascii=False))


async def cmd_download(args):
    """
    Download media from a specific message.
    --message-id targets the message with media attached.
    --out sets the output directory (default: current dir).
    Output: JSON object with the downloaded file path.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        # Fetch the specific message by ID
        msgs = await client.get_messages(entity, ids=args.message_id)
        msg = msgs if not isinstance(msgs, list) else msgs[0] if msgs else None
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)
        if not msg.media:
            print(json.dumps({"error": f"Message {args.message_id} has no media"}), file=sys.stderr)
            sys.exit(1)
        # Ensure output directory exists
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Download the media file to the output directory
        path = await client.download_media(msg, file=str(out_dir))
        print(json.dumps({"ok": True, "path": str(path)}, indent=2))


async def cmd_upload(args):
    """
    Upload and send a file (photo, video, document) to a chat.
    --file is the local path to the file.
    --caption is optional text sent alongside the file.
    Output: JSON object of the sent message with media.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        file_path = Path(args.file)
        if not file_path.exists():
            print(json.dumps({"error": f"File not found: {args.file}"}), file=sys.stderr)
            sys.exit(1)
        # voice_note=True sends the file as a playable voice message in Telegram
        msg = await client.send_file(entity, str(file_path), caption=args.caption or "", voice_note=getattr(args, 'voice_note', False))
        print(json.dumps(serialize_message(msg), indent=2, ensure_ascii=False))


async def cmd_pinned(args):
    """
    Fetch pinned messages from a chat/channel.
    --limit caps the number returned (default 10).
    Output: JSON array of pinned message objects.
    """
    from telethon.tl.types import InputMessagesFilterPinned

    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        # search with pinned filter returns only pinned messages
        messages = await client.get_messages(entity, limit=args.limit, filter=InputMessagesFilterPinned)
        result = [serialize_message(m) for m in messages]
        print(json.dumps(result, indent=2, ensure_ascii=False))


async def cmd_buttons(args):
    """
    Inspect inline keyboard buttons on a specific message.
    Fetches a single message by ID and returns its button layout as JSON.
    Each button includes row/col indices (for use with the 'click' command),
    the visible text label, button type, and type-specific data (callback data, URL, etc.).
    Output: JSON object with message_id and buttons array, or error if no buttons found.
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        # --- Fetch the single message by its ID ---
        msg = await client.get_messages(entity, ids=args.message_id)
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)

        buttons = serialize_buttons(msg.reply_markup)
        if not buttons:
            print(json.dumps({"error": f"Message {args.message_id} has no inline buttons"}), file=sys.stderr)
            sys.exit(1)

        print(json.dumps({
            "message_id": msg.id,
            "chat": str(args.chat),
            "buttons": buttons
        }, indent=2, ensure_ascii=False))


async def cmd_click(args):
    """
    Click an inline keyboard button on a bot message.
    Targets a button by its row and column index (0-based, as shown by 'buttons' command).
    Uses Telethon's Message.click(row, col) which handles the underlying
    GetBotCallbackAnswerRequest automatically.
    Output: JSON object with the bot's callback response (message and/or alert text).
    """
    client = make_client()
    async with client:
        entity = await resolve_entity(client, args.chat)
        # --- Fetch the target message ---
        msg = await client.get_messages(entity, ids=args.message_id)
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)

        if not msg.reply_markup or not hasattr(msg.reply_markup, "rows"):
            print(json.dumps({"error": f"Message {args.message_id} has no clickable buttons"}), file=sys.stderr)
            sys.exit(1)

        # --- Validate row/col bounds before clicking ---
        rows = msg.reply_markup.rows
        if args.row >= len(rows):
            print(json.dumps({"error": f"Row {args.row} out of range (message has {len(rows)} rows)"}), file=sys.stderr)
            sys.exit(1)
        if args.col >= len(rows[args.row].buttons):
            print(json.dumps({"error": f"Col {args.col} out of range (row {args.row} has {len(rows[args.row].buttons)} buttons)"}), file=sys.stderr)
            sys.exit(1)

        # --- Click the button; Telethon sends the callback query to the bot ---
        result = await msg.click(args.row, args.col)

        # --- Build response from the callback answer ---
        response = {
            "ok": True,
            "chat": str(args.chat),
            "message_id": args.message_id,
            "button_text": rows[args.row].buttons[args.col].text,
            "row": args.row,
            "col": args.col,
        }

        # result can be a MessagesBotCallbackAnswer or an updated Message
        if result is None:
            response["callback_response"] = None
        elif hasattr(result, "message") and hasattr(result, "alert"):
            # BotCallbackAnswer: contains optional message/alert from the bot
            response["callback_response"] = {
                "message": result.message,
                "alert": result.alert,
                "url": getattr(result, "url", None),
            }
        else:
            # Updated message returned (e.g., URL button opened, or message edited)
            response["callback_response"] = serialize_message(result) if hasattr(result, "id") else str(result)

        print(json.dumps(response, indent=2, ensure_ascii=False))


async def cmd_search_global(args):
    """
    Search messages globally across all chats.
    --query is the search term, --limit caps results (default 20).
    Output: JSON array of message objects with an added 'chat_id' field.
    """
    client = make_client()
    async with client:
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty

        results = []
        async for msg in client.iter_messages(None, search=args.query, limit=args.limit):
            data = serialize_message(msg)
            data["chat_id"] = msg.chat_id
            results.append(data)
        print(json.dumps(results, indent=2, ensure_ascii=False))


# ============================================================
# CLI argument parser — maps subcommands to handler functions
# ============================================================

def build_parser():
    """
    Build the argparse parser with all subcommands and their arguments.
    Each subcommand maps to an async handler function via set_defaults(func=...).
    """
    parser = argparse.ArgumentParser(description="Telegram CLI client for Claude Code")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- dialogs: list recent chats ---
    p = sub.add_parser("dialogs", help="List recent dialogs")
    p.add_argument("--limit", type=int, default=20, help="Max dialogs to fetch")
    p.set_defaults(func=cmd_dialogs)

    # --- topics: list forum topics in a chat ---
    p = sub.add_parser("topics", help="List forum topics in a chat")
    p.add_argument("chat", help="Chat identifier (must be a forum-enabled group/channel)")
    p.add_argument("--limit", type=int, default=1000, help="Max messages to scan for topic discovery (default 1000)")
    p.set_defaults(func=cmd_topics)

    # --- messages: fetch messages from a chat ---
    p = sub.add_parser("messages", help="Get messages from a chat")
    p.add_argument("chat", help="Chat identifier (username, phone, ID, or invite link)")
    p.add_argument("--limit", type=int, default=20, help="Max messages to fetch")
    p.add_argument("--search", help="Filter messages by text content")
    p.add_argument("--min-id", type=int, help="Only messages with ID greater than this")
    p.add_argument("--max-id", type=int, help="Only messages with ID less than this")
    p.add_argument("--topic", type=int, help="Forum topic ID to filter messages by")
    p.set_defaults(func=cmd_messages)

    # --- send: send a text message ---
    p = sub.add_parser("send", help="Send a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--text", required=True, help="Message text to send")
    p.add_argument("--topic", type=int, help="Forum topic ID to send into")
    p.set_defaults(func=cmd_send)

    # --- reply: reply to a specific message ---
    p = sub.add_parser("reply", help="Reply to a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to reply to")
    p.add_argument("--text", required=True, help="Reply text")
    p.set_defaults(func=cmd_reply)

    # --- forward: forward a message between chats ---
    p = sub.add_parser("forward", help="Forward a message")
    p.add_argument("from_chat", help="Source chat identifier")
    p.add_argument("to_chat", help="Destination chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to forward")
    p.set_defaults(func=cmd_forward)

    # --- edit: edit an existing message ---
    p = sub.add_parser("edit", help="Edit a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to edit")
    p.add_argument("--text", required=True, help="New message text")
    p.set_defaults(func=cmd_edit)

    # --- delete: delete messages by ID ---
    p = sub.add_parser("delete", help="Delete messages")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-ids", required=True, help="Comma-separated message IDs to delete")
    p.set_defaults(func=cmd_delete)

    # --- react: send a reaction to a message ---
    p = sub.add_parser("react", help="React to a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to react to")
    p.add_argument("--emoji", required=True, help="Reaction emoji (e.g. 👍)")
    p.set_defaults(func=cmd_react)

    # --- info: get entity details ---
    p = sub.add_parser("info", help="Get entity info")
    p.add_argument("entity", help="Entity identifier (username, phone, ID)")
    p.set_defaults(func=cmd_info)

    # --- participants: list group/channel members ---
    p = sub.add_parser("participants", help="List participants")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--limit", type=int, default=50, help="Max participants to fetch")
    p.add_argument("--search", help="Filter participants by name/username")
    p.set_defaults(func=cmd_participants)

    # --- download: download media from a message ---
    p = sub.add_parser("download", help="Download media from a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message with media")
    p.add_argument("--out", default=".", help="Output directory (default: current dir)")
    p.set_defaults(func=cmd_download)

    # --- upload: send a file to a chat ---
    p = sub.add_parser("upload", help="Upload a file to a chat")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--file", required=True, help="Path to file to upload")
    p.add_argument("--caption", help="Caption for the uploaded file")
    p.add_argument("--voice-note", action="store_true", help="Send as a voice message")
    p.set_defaults(func=cmd_upload)

    # --- pinned: get pinned messages ---
    p = sub.add_parser("pinned", help="Get pinned messages")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--limit", type=int, default=10, help="Max pinned messages to fetch")
    p.set_defaults(func=cmd_pinned)

    # --- search-global: search messages across all chats ---
    p = sub.add_parser("search-global", help="Search messages globally")
    p.add_argument("--query", required=True, help="Search query")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.set_defaults(func=cmd_search_global)

    # --- buttons: inspect inline buttons on a message ---
    p = sub.add_parser("buttons", help="Show inline buttons on a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to inspect")
    p.set_defaults(func=cmd_buttons)

    # --- click: click an inline button on a bot message ---
    p = sub.add_parser("click", help="Click an inline button on a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message with buttons")
    p.add_argument("--row", type=int, default=0, help="Button row index, 0-based (default: 0)")
    p.add_argument("--col", type=int, default=0, help="Button column index, 0-based (default: 0)")
    p.set_defaults(func=cmd_click)

    return parser


def main():
    """
    Entry point. Parses CLI args, dispatches to the appropriate async handler,
    and ensures errors are reported as JSON to stderr.
    """
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except Exception as e:
        print(json.dumps({"error": str(e), "type": type(e).__name__}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
