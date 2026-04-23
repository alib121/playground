#!/usr/bin/env python3
"""
Personal Admin Agent — runtime session.

Each run starts a fresh Managed Agent session. The agent automatically
reads your persistent memory from the previous session and saves an
updated copy when you exit.

Usage:
    python admin_agent.py                     # interactive chat
    python admin_agent.py "Draft a weekly standup for me"  # kick off with a task
    echo "Summarise my notes" | python admin_agent.py -   # pipe a message in
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import anthropic

CONFIG_FILE = Path("admin_config.json")
MEMORY_FILE = Path("admin_memories.md")
MEMORY_MOUNT_PATH = "/workspace/memories.md"
MEMORY_OUTPUT_PATH = "memories.md"  # filename written by the agent under /mnt/session/outputs/


# ──────────────────────────────────────────────────────────────────────────────
# Config / memory helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(
            f"Error: {CONFIG_FILE} not found.\n"
            "Run setup.py first to create the agent and environment.",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def upload_memory(client: anthropic.Anthropic) -> Optional[str]:
    """Upload the local memory file and return its file_id, or None if absent."""
    if not MEMORY_FILE.exists():
        return None
    with MEMORY_FILE.open("rb") as fh:
        uploaded = client.beta.files.upload(file=(MEMORY_FILE.name, fh, "text/markdown"))
    return uploaded.id


def save_memory(client: anthropic.Anthropic, session_id: str) -> None:
    """Download the agent's updated memory file from session outputs, if written."""
    # The Files API indexes session outputs with a brief lag after idle.
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
                    print(f"\n[Memory saved → {MEMORY_FILE}]")
                    return
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2)

    # The agent may not have updated memory this session — that's fine.


def delete_file_quietly(client: anthropic.Anthropic, file_id: str) -> None:
    try:
        client.beta.files.delete(file_id)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Streaming helper
# ──────────────────────────────────────────────────────────────────────────────

def stream_turn(client: anthropic.Anthropic, session_id: str, message: str) -> None:
    """Send *message*, stream the agent's response, block until the turn is done."""
    with client.beta.sessions.stream(session_id=session_id) as stream:
        # Send the message while the stream is open so we catch the earliest events.
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": message}],
                }
            ],
        )

        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        print(block.text, end="", flush=True)

            elif event.type == "agent.thinking":
                # Thinking is hidden by default on Opus 4.7.
                # Uncomment the lines below to show it:
                # for block in event.content:
                #     if block.type == "thinking":
                #         print(f"\n[thinking: {block.thinking[:120]}…]", flush=True)
                pass

            elif event.type == "session.status_terminated":
                print("\n[Session terminated by server]")
                break

            elif event.type == "session.status_idle":
                # requires_action means the agent is waiting for a custom tool result
                # (not applicable here since we have no custom tools).
                if event.stop_reason.type != "requires_action":
                    break

    print()  # newline after streamed output


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run(initial_message: Optional[str] = None) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    config = load_config()

    # ── Upload memory ──
    resources = []
    memory_file_id: Optional[str] = None

    memory_file_id = upload_memory(client)
    if memory_file_id:
        resources.append(
            {
                "type": "file",
                "file_id": memory_file_id,
                "mount_path": MEMORY_MOUNT_PATH,
            }
        )
        print(f"[Memory loaded from {MEMORY_FILE}]")
    else:
        print("[No memory file yet — starting fresh]")

    # ── Create session ──
    session = client.beta.sessions.create(
        agent={
            "type": "agent",
            "id": config["agent_id"],
            "version": config["agent_version"],
        },
        environment_id=config["environment_id"],
        title="Admin session",
        resources=resources or None,
    )
    print(f"[Session: {session.id}]\n")

    # ── Initial message (optional) ──
    if initial_message:
        print(f"You: {initial_message}\n")
        print("Assistant: ", end="", flush=True)
        stream_turn(client, session.id, initial_message)

    # ── Interactive loop ──
    print("Type your message and press Enter. Use Ctrl-D or type 'exit' to quit.\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except EOFError:
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "bye"):
                break

            print("\nAssistant: ", end="", flush=True)
            stream_turn(client, session.id, user_input)

    except KeyboardInterrupt:
        print("\n[Interrupted]")

    # ── Persist memory ──
    print("\n[Saving memory…]")
    save_memory(client, session.id)

    # ── Clean up the uploaded input memory file ──
    if memory_file_id:
        delete_file_quietly(client, memory_file_id)

    print("[Session closed]")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Personal Admin Agent — interactive chat with persistent memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Optional first message to kick off the session",
    )
    args = parser.parse_args()

    # Support `echo '...' | python admin_agent.py -`
    if args.message == "-":
        initial = sys.stdin.read().strip()
    else:
        initial = args.message

    run(initial)


if __name__ == "__main__":
    main()
