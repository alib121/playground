#!/usr/bin/env python3
"""
ONE-TIME SETUP — run once, then store admin_config.json safely.

Creates the Managed Agent + Environment on Anthropic's infrastructure.
The returned IDs are saved to admin_config.json and reused by admin_agent.py
on every subsequent session.

Usage:
    pip install -r requirements.txt
    ANTHROPIC_API_KEY=sk-... python setup.py
"""

import json
import os
import sys
from pathlib import Path

import anthropic

CONFIG_FILE = Path("admin_config.json")

SYSTEM_PROMPT = """\
You are a highly capable personal admin assistant with persistent memory and broad agency.

## MEMORY
At session start, read /workspace/memories.md if it exists — it contains context from \
previous sessions: user preferences, ongoing projects, important facts, and notes.

After every session (or whenever you learn something the user would want remembered), \
write a comprehensive, updated memory file to /mnt/session/outputs/memories.md. \
Structure it with clear markdown sections:
- **User Preferences** — communication style, tools, recurring patterns
- **Ongoing Projects** — status of active work
- **Key Facts** — important context about the user's work and life
- **Notes** — anything else worth preserving

Always merge new information with what was already in the memory file rather than \
discarding prior content.

## YOUR ROLE
You are a proactive personal admin. Your capabilities include:
- **Research** — web search and fetching URLs for current information
- **Drafting** — emails, documents, summaries, reports
- **Organization** — structuring information, creating plans, managing lists
- **Analysis** — processing data, synthesising sources, comparing options
- **File work** — reading, writing, and editing files in your workspace

## HOW TO WORK
- Use tools proactively. If a task benefits from web search, search without being asked.
- Be direct and concrete. Give actionable output, not vague suggestions.
- When uncertain about what the user wants, make a reasonable attempt and ask to refine.
- For longer tasks, narrate your progress so the user knows what you're doing.
- Keep responses focused — avoid padding and preamble.
"""


def main() -> None:
    if CONFIG_FILE.exists():
        existing = json.loads(CONFIG_FILE.read_text())
        print(f"Config already exists at {CONFIG_FILE}:")
        print(json.dumps(existing, indent=2))
        answer = input("\nRecreate? This will create NEW agent + environment objects. [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted — keeping existing config.")
            return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("Creating cloud environment (unrestricted networking)…")
    environment = client.beta.environments.create(
        name="admin-agent-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
    )
    print(f"  Environment: {environment.id}")

    print("Creating agent…")
    agent = client.beta.agents.create(
        name="Personal Admin Agent",
        model="claude-opus-4-7",
        system=SYSTEM_PROMPT,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": {"enabled": True},
            }
        ],
    )
    print(f"  Agent:       {agent.id}  (version {agent.version})")

    config = {
        "agent_id": agent.id,
        "agent_version": agent.version,
        "environment_id": environment.id,
    }

    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nSetup complete. Config saved to {CONFIG_FILE}")
    print("\nNext step:")
    print("  python admin_agent.py")


if __name__ == "__main__":
    main()
