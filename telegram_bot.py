#!/usr/bin/env python3
"""
Telegram interface for Maude — Ali's personal assistant.

Each Telegram chat gets its own Maude session with persistent memory.
Memory is loaded at session start and saved when the session ends.

Setup:
    1. Message @BotFather on Telegram → /newbot → copy the token
    2. Set environment variables:
         ANTHROPIC_API_KEY=sk-...
         TELEGRAM_BOT_TOKEN=...
         ALLOWED_CHAT_IDS=123456789   # optional: comma-separated chat IDs
    3. pip install -r requirements.txt
    4. python setup.py          # if you haven't already
    5. python telegram_bot.py

Finding your chat ID:
    Start the bot without ALLOWED_CHAT_IDS, send it a message, and check the
    logs — your chat ID is printed there. Then restart with ALLOWED_CHAT_IDS set.

Commands:
    /start  — start or restart a fresh session
    /end    — end the session and save memory
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

CONFIG_FILE = Path("admin_config.json")
MEMORY_FILE = Path("admin_memories.md")
MEMORY_MOUNT_PATH = "/workspace/memories.md"
MEMORY_OUTPUT_PATH = "memories.md"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# chat_id -> {"session_id": str, "memory_file_id": str | None}
_sessions: dict[int, dict] = {}

_executor = ThreadPoolExecutor(max_workers=4)


# ── Synchronous Anthropic helpers (run in executor) ───────────────────────────

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} not found. Run setup.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def _upload_memory(client: anthropic.Anthropic) -> Optional[str]:
    if not MEMORY_FILE.exists():
        return None
    with MEMORY_FILE.open("rb") as fh:
        uploaded = client.beta.files.upload(file=(MEMORY_FILE.name, fh, "text/markdown"))
    return uploaded.id


def _create_session(client: anthropic.Anthropic, config: dict, memory_file_id: Optional[str]) -> str:
    resources = []
    if memory_file_id:
        resources.append({
            "type": "file",
            "file_id": memory_file_id,
            "mount_path": MEMORY_MOUNT_PATH,
        })
    session = client.beta.sessions.create(
        agent={"type": "agent", "id": config["agent_id"], "version": config["agent_version"]},
        environment_id=config["environment_id"],
        title="Telegram session",
        resources=resources or None,
    )
    return session.id


def _send_and_collect(client: anthropic.Anthropic, session_id: str, message: str) -> str:
    parts: list[str] = []
    client.beta.sessions.events.send(
        session_id=session_id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
    )
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        parts.append(block.text)
            elif event.type == "session.status_terminated":
                break
            elif event.type == "session.status_idle":
                if event.stop_reason.type != "requires_action":
                    break
    return "".join(parts)


_WHATSAPP_RE = re.compile(
    r"whatsapp|whats\s*app|school\s*chat|p&c|group\s*chat", re.IGNORECASE
)


def _fetch_whatsapp_context(message: str) -> str:
    """If the message mentions WhatsApp, fetch recent group messages as context."""
    if not _WHATSAPP_RE.search(message):
        return ""
    url = os.environ.get("WHATSAPP_SERVICE_URL", "").rstrip("/")
    if not url:
        return ""
    since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        req_url = f"{url}/messages?since={urllib.parse.quote(since)}&limit=300"
        with urllib.request.urlopen(req_url, timeout=5) as resp:
            msgs = json.loads(resp.read())
        if not msgs:
            return ""
        lines = [f"[{m['time']} {m['chat_name']} — {m['sender_name']}]: {m['text']}" for m in msgs]
        return "[Recent WhatsApp group messages — last 14 days:\n" + "\n".join(lines) + "\n]"
    except Exception as exc:
        logger.warning("WhatsApp service unavailable: %s", exc)
        return ""


def _save_memory(client: anthropic.Anthropic, session_id: str) -> bool:
    for attempt in range(3):
        try:
            files = client.beta.files.list(
                scope_id=session_id,
                betas=["managed-agents-2026-04-01"],
            )
            for f in files.data:
                if Path(f.filename).name == MEMORY_OUTPUT_PATH:
                    content = client.beta.files.download(f.id)
                    MEMORY_FILE.write_bytes(content.read())
                    return True
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2)
    return False


def _delete_file(client: anthropic.Anthropic, file_id: str) -> None:
    try:
        client.beta.files.delete(file_id)
    except Exception:
        pass


# ── Async session management ──────────────────────────────────────────────────

async def _open_session(chat_id: int, client: anthropic.Anthropic, config: dict) -> str:
    loop = asyncio.get_event_loop()
    memory_file_id = await loop.run_in_executor(_executor, _upload_memory, client)
    session_id = await loop.run_in_executor(_executor, _create_session, client, config, memory_file_id)
    _sessions[chat_id] = {"session_id": session_id, "memory_file_id": memory_file_id}
    logger.info("Opened session %s for chat %s", session_id, chat_id)
    return session_id


async def _close_session(chat_id: int, client: anthropic.Anthropic) -> bool:
    state = _sessions.pop(chat_id, None)
    if not state:
        return False
    loop = asyncio.get_event_loop()
    saved = await loop.run_in_executor(_executor, _save_memory, client, state["session_id"])
    if state.get("memory_file_id"):
        await loop.run_in_executor(_executor, _delete_file, client, state["memory_file_id"])
    logger.info("Closed session %s (memory saved: %s)", state["session_id"], saved)
    return saved


# ── Access control ────────────────────────────────────────────────────────────

def _is_allowed(chat_id: int, allowed: Optional[set[int]]) -> bool:
    if allowed is None:
        return True
    return chat_id in allowed


async def _reject(update: Update) -> None:
    logger.warning("Blocked message from chat %s", update.effective_chat.id)
    await update.message.reply_text("Sorry, I don't know you.")


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    allowed: Optional[set[int]] = context.bot_data["allowed_ids"]
    if not _is_allowed(chat_id, allowed):
        await _reject(update)
        return

    client: anthropic.Anthropic = context.bot_data["client"]
    config: dict = context.bot_data["config"]

    if chat_id in _sessions:
        await update.message.reply_text("Ending current session…")
        await _close_session(chat_id, client)

    await update.message.reply_text("Starting up…")
    await _open_session(chat_id, client, config)
    await update.message.reply_text("Ready. What do you need?")


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    allowed: Optional[set[int]] = context.bot_data["allowed_ids"]
    if not _is_allowed(chat_id, allowed):
        await _reject(update)
        return

    client: anthropic.Anthropic = context.bot_data["client"]
    if chat_id not in _sessions:
        await update.message.reply_text("No active session.")
        return

    await update.message.reply_text("Saving memory and closing…")
    saved = await _close_session(chat_id, client)
    await update.message.reply_text(
        "Done. Memory saved." if saved else "Done. (Nothing new to save this session.)"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    allowed: Optional[set[int]] = context.bot_data["allowed_ids"]

    if not _is_allowed(chat_id, allowed):
        logger.info("Unrecognised chat_id: %s", chat_id)
        await _reject(update)
        return

    client: anthropic.Anthropic = context.bot_data["client"]
    config: dict = context.bot_data["config"]
    text = update.message.text

    if chat_id not in _sessions:
        await _open_session(chat_id, client, config)

    session_id = _sessions[chat_id]["session_id"]
    loop = asyncio.get_event_loop()

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Inject WhatsApp context if the message is asking about WhatsApp
    wa_context = await loop.run_in_executor(_executor, _fetch_whatsapp_context, text)
    augmented_text = f"{wa_context}\n\nUser: {text}" if wa_context else text

    try:
        response = await loop.run_in_executor(
            _executor, _send_and_collect, client, session_id, augmented_text
        )
    except Exception as exc:
        logger.warning("Session error (%s) — recreating and retrying", exc)
        _sessions.pop(chat_id, None)
        await _open_session(chat_id, client, config)
        session_id = _sessions[chat_id]["session_id"]
        try:
            response = await loop.run_in_executor(
                _executor, _send_and_collect, client, session_id, augmented_text
            )
        except Exception as exc2:
            logger.error("Retry failed: %s", exc2)
            await update.message.reply_text("Something went wrong — please try again.")
            return

    await update.message.reply_text(response or "(No response)")


async def _on_shutdown(app: Application) -> None:
    client: anthropic.Anthropic = app.bot_data.get("client")
    if not client:
        return
    for chat_id in list(_sessions.keys()):
        logger.info("Shutdown: saving memory for chat %s", chat_id)
        await _close_session(chat_id, client)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("Error: TELEGRAM_BOT_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    raw_ids = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
    allowed_ids: Optional[set[int]] = None
    if raw_ids:
        try:
            allowed_ids = {int(x.strip()) for x in raw_ids.split(",") if x.strip()}
        except ValueError:
            print("Error: ALLOWED_CHAT_IDS must be comma-separated integers.", file=sys.stderr)
            sys.exit(1)

    if allowed_ids is None:
        logger.warning(
            "ALLOWED_CHAT_IDS not set — anyone can message this bot. "
            "Set it to your chat ID to restrict access."
        )

    client = anthropic.Anthropic(api_key=api_key)
    config = _load_config()

    app = (
        Application.builder()
        .token(bot_token)
        .post_shutdown(_on_shutdown)
        .build()
    )
    app.bot_data["client"] = client
    app.bot_data["config"] = config
    app.bot_data["allowed_ids"] = allowed_ids

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Maude Telegram bot running (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
