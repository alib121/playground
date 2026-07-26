#!/usr/bin/env python3
"""
Google Calendar helper for Maude.
Run `python calendar_helper.py auth` once on your Mac to authenticate.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
CREDENTIALS_FILE = Path("calendar_credentials.json")
TOKEN_FILE = Path("calendar_token.json")
TZ_NAME = os.environ.get("CALENDAR_TIMEZONE", "Australia/Sydney")


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None

    # Also accept token from env var (for Railway)
    token_env = os.environ.get("GOOGLE_CALENDAR_TOKEN")
    if token_env and not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(token_env)

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            if not CREDENTIALS_FILE.exists():
                print(f"Error: {CREDENTIALS_FILE} not found.", file=sys.stderr)
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
            TOKEN_FILE.write_text(creds.to_json())

    return creds


def list_events(days_behind=0, days_ahead=7):
    from googleapiclient.discovery import build
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_behind)).isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    return result.get("items", [])


def create_event(summary, start_dt, end_dt=None, description=""):
    from googleapiclient.discovery import build
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)

    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TZ_NAME},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TZ_NAME},
    }

    return service.events().insert(calendarId="primary", body=event).execute()


def format_events_for_context(events) -> str:
    if not events:
        return ""
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        # Trim to readable format
        if "T" in start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start = dt.strftime("%a %d %b %Y %H:%M")
            except Exception:
                pass
        lines.append(f"- {start}: {e.get('summary', '(no title)')}")
    return "[Upcoming calendar events:\n" + "\n".join(lines) + "\n]"


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("auth", "list", "token"):
        print("Usage: python calendar_helper.py auth|list|token")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "auth":
        get_credentials()
        print(f"✓ Authenticated. Token saved to {TOKEN_FILE}")
        print("\nNext step — copy the token for Railway:")
        print("  python calendar_helper.py token")

    elif cmd == "list":
        events = list_events(days_behind=1, days_ahead=7)
        if not events:
            print("No events found.")
        else:
            print(format_events_for_context(events))

    elif cmd == "token":
        if not TOKEN_FILE.exists():
            print("Run `python calendar_helper.py auth` first.")
            sys.exit(1)
        print(TOKEN_FILE.read_text())
        print("\nCopy the JSON above and set it as GOOGLE_CALENDAR_TOKEN in Railway.")
