#!/usr/bin/env python3
"""
Gmail helper for Maude's email agent.

Usage:
    python gmail_helper.py auth                        # first-time auth
    python gmail_helper.py list [--max N] [--query Q]  # list emails
    python gmail_helper.py read <message_id>           # read full email
    python gmail_helper.py draft <to> <subject> <body> # save a draft
"""

import argparse
import base64
import json
import os
import sys
from email.mime.text import MIMEText
from pathlib import Path

CREDENTIALS_FILE = Path("gmail_credentials.json")
TOKEN_FILE = Path("gmail_token.json")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


def get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(
                    f"Error: {CREDENTIALS_FILE} not found.\n"
                    "Download OAuth credentials from Google Cloud Console and save as gmail_credentials.json.\n"
                    "See: https://console.cloud.google.com/apis/credentials",
                    file=sys.stderr,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def cmd_auth(_args):
    """Authenticate and store token."""
    get_service()
    print("Auth successful. Token saved to gmail_token.json")


def _parse_headers(headers: list) -> dict:
    return {h["name"]: h["value"] for h in headers}


def _decode_body(payload: dict) -> str:
    """Extract plain text body from a message payload."""
    def _extract(part):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            result = _extract(sub)
            if result:
                return result
        return ""

    return _extract(payload)


def cmd_list(args):
    service = get_service()
    query = args.query or "is:unread"
    result = service.users().messages().list(
        userId="me", q=query, maxResults=args.max
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        print("No messages found.")
        return

    for msg in messages:
        detail = service.users().messages().get(
            userId="me", messageId=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        headers = _parse_headers(detail["payload"]["headers"])
        snippet = detail.get("snippet", "")[:100]
        print(f"ID:      {msg['id']}")
        print(f"From:    {headers.get('From', '—')}")
        print(f"Subject: {headers.get('Subject', '—')}")
        print(f"Date:    {headers.get('Date', '—')}")
        print(f"Snippet: {snippet}")
        print()


def cmd_read(args):
    service = get_service()
    msg = service.users().messages().get(
        userId="me", messageId=args.message_id, format="full"
    ).execute()
    headers = _parse_headers(msg["payload"]["headers"])
    body = _decode_body(msg["payload"])

    print(f"From:    {headers.get('From', '—')}")
    print(f"To:      {headers.get('To', '—')}")
    print(f"Subject: {headers.get('Subject', '—')}")
    print(f"Date:    {headers.get('Date', '—')}")
    print()
    print(body or "[No plain-text body]")


def cmd_draft(args):
    service = get_service()
    mime = MIMEText(args.body)
    mime["to"] = args.to
    mime["subject"] = args.subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    print(f"Draft saved (id: {draft['id']}). Review and send from Gmail.")


def main():
    parser = argparse.ArgumentParser(description="Gmail helper for Maude")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth", help="Authenticate with Gmail")

    p_list = sub.add_parser("list", help="List emails")
    p_list.add_argument("--max", type=int, default=10)
    p_list.add_argument("--query", "-q", default="is:unread", help="Gmail search query")

    p_read = sub.add_parser("read", help="Read a full email")
    p_read.add_argument("message_id")

    p_draft = sub.add_parser("draft", help="Save a draft reply")
    p_draft.add_argument("to")
    p_draft.add_argument("subject")
    p_draft.add_argument("body")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"auth": cmd_auth, "list": cmd_list, "read": cmd_read, "draft": cmd_draft}[
        args.command
    ](args)


if __name__ == "__main__":
    main()
