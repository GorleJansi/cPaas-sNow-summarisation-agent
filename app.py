"""
main.py — Webex + ServiceNow case-summary bot
=============================================
Flow overview:
  1. User DMs the bot → /webhook/webex fires
  2. If first time seeing this room → send welcome card
  3. Route the message (bare case number / "summarize X" / anything else)
  4. Adaptive card submit → /webhook/webex/card-action fires
  5. Fetch case from ServiceNow, build timeline, call LLM, flip card to summary

Debug legend in logs:
  ✅  success / happy path
  ❌  skipped / error
  ⏩  step transition
  🆕  first-time room / welcome
  📨  outbound message to Webex
  🔍  data fetch (Webex API or ServiceNow)
"""

import json
import os
import re
import time
import threading
from typing import Any, Dict, Optional, Set

import boto3
import requests
from fastapi import FastAPI, Request

from config import WEBEX_BOT_TOKEN, WEBEX_BOT_EMAIL
from servicenow_client import get_case_by_number, get_case_journal_entries, get_case_emails
from formatter import build_timeline
from summarizer import summarize_case_with_llm

# Lambda client for async self-invocation
_lambda_client = None
_FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")


def _invoke_summary_async(room_id: str, case_number: str, card_message_id: Optional[str]) -> None:
    """
    Fire-and-forget: invoke THIS Lambda asynchronously with a summary event.
    The current request returns 200 immediately; the heavy pipeline runs
    in a separate Lambda invocation with the full 120-second timeout.
    """
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")

    payload = {
        "_async_summary": True,
        "room_id": room_id,
        "case_number": case_number,
        "card_message_id": card_message_id,
    }
    print(f"  [ASYNC] 🚀 Invoking Lambda async for {case_number}")
    _lambda_client.invoke(
        FunctionName=_FUNCTION_NAME,
        InvocationType="Event",        # async — returns immediately
        Payload=json.dumps(payload).encode(),
    )
    print(f"  [ASYNC] ✅ Async invocation dispatched for {case_number}")

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

app = FastAPI()

WEBEX_API_BASE = "https://webexapis.com/v1"
BOT_EMAIL_LOWER = (WEBEX_BOT_EMAIL or "").lower()

# In-memory set of room IDs we have already welcomed.
# Resets on server restart — that's fine; a repeat welcome on restart is harmless.
welcomed_rooms: Set[str] = set()


# ---------------------------------------------------------------------------
# STEP 0 — Low-level utilities
# ---------------------------------------------------------------------------

def is_bot_message(email: str) -> bool:
    """Return True if the email belongs to any bot account (including this bot)."""
    if not email:
        return False
    if BOT_EMAIL_LOWER and email.lower() == BOT_EMAIL_LOWER:
        return True
    # Webex bot email conventions
    if email.endswith(".bot"):
        return True
    if "@webex.bot" in email:
        return True
    if "bot@webex" in email or "bot@cisco" in email:
        return True
    return False


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
    """
    Wrapper around requests with retry logic.
    Returns None on 404 (bot not in room / resource deleted).
    Raises on other errors after max_retries.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
            print(f"  [HTTP] {method} {url} → {resp.status_code}")

            if resp.status_code == 404:
                # 404 usually means the bot is not a member of this room
                # or the resource was already deleted. Not a crash — return None.
                print(f"  [HTTP] ⚠️  404 — bot likely not in this room or resource gone.")
                return None

            if resp.status_code == 405:
                # 405 = Method Not Allowed — bot may lack edit permissions.
                print(f"  [HTTP] ⚠️  405 — PATCH not allowed (bot lacks edit permission).")
                return None

            resp.raise_for_status()
            return resp

        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"  [HTTP] ❌ attempt {attempt}/{max_retries} failed: {repr(exc)}")
            if attempt < max_retries:
                time.sleep(1.5 * attempt)

    raise last_exc


# ---------------------------------------------------------------------------
# STEP 1 — Webex API calls
# ---------------------------------------------------------------------------

def get_webex_message(message_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single message by ID.
    Returns None if the bot cannot access it (not in room, deleted, etc.).
    """
    print(f"  [STEP 1 / get_webex_message] 🔍 Fetching message_id={message_id}")
    resp = _request("GET", f"{WEBEX_API_BASE}/messages/{message_id}", headers=_headers())
    if resp:
        data = resp.json()
        print(f"  [STEP 1 / get_webex_message] ✅ email={data.get('personEmail')} "
              f"text={repr((data.get('text') or '')[:80])}")
        return data
    print(f"  [STEP 1 / get_webex_message] ❌ Could not fetch message — bot not in room?")
    return None


def get_attachment_action(action_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the inputs submitted via an Adaptive Card."""
    print(f"  [STEP 1 / get_attachment_action] 🔍 Fetching action_id={action_id}")
    resp = _request("GET", f"{WEBEX_API_BASE}/attachment/actions/{action_id}", headers=_headers())
    if resp:
        print(f"  [STEP 1 / get_attachment_action] ✅ Action details fetched")
        return resp.json()
    print(f"  [STEP 1 / get_attachment_action] ❌ Could not fetch action details")
    return None


def send_text(room_id: str, text: str) -> None:
    """Send a plain-text message to a room."""
    print(f"  [send_text] 📨 room={room_id} | text={repr(text[:80])}")
    resp = _request(
        "POST",
        f"{WEBEX_API_BASE}/messages",
        headers=_headers(),
        json={"roomId": room_id, "text": text},
    )
    print(f"  [send_text] {'✅ Sent' if resp else '❌ Failed'}")


def send_card(room_id: str, card_content: Dict[str, Any], fallback_text: str = "Card") -> Optional[str]:
    """
    Send an Adaptive Card to a room.
    Returns the message ID of the sent card (needed later for replace_card).
    """
    print(f"  [send_card] 📨 room={room_id} | fallback={repr(fallback_text)}")
    resp = _request(
        "POST",
        f"{WEBEX_API_BASE}/messages",
        headers=_headers(),
        json={
            "roomId": room_id,
            "text": fallback_text,
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_content,
            }],
        },
    )
    if resp:
        card_id = resp.json().get("id")
        print(f"  [send_card] ✅ Card sent — card_id={card_id}")
        return card_id
    print(f"  [send_card] ❌ Failed to send card")
    return None


def replace_card(message_id: str, card_content: Dict[str, Any], fallback_text: str = "Card", room_id: str = "") -> None:
    """
    Replace (PATCH) an existing Adaptive Card in-place.
    Used to flip the working-card to summary-card without a new message.
    Falls back to sending a new card if PATCH is not allowed (405).
    """
    print(f"  [replace_card] 📨 Replacing card message_id={message_id} | fallback={repr(fallback_text)}")
    resp = _request(
        "PATCH",
        f"{WEBEX_API_BASE}/messages/{message_id}",
        headers=_headers(),
        json={
            "text": fallback_text,
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card_content,
            }],
        },
    )
    if resp:
        print(f"  [replace_card] ✅ Card replaced")
    elif room_id:
        print(f"  [replace_card] ⚠️ PATCH failed — sending new card instead")
        send_card(room_id, card_content, fallback_text=fallback_text)
    else:
        print(f"  [replace_card] ❌ Replace failed and no room_id for fallback")


# ---------------------------------------------------------------------------
# STEP 2 — Adaptive Card templates
# ---------------------------------------------------------------------------

def _welcome_card(user_email: str = "") -> Dict[str, Any]:
    """
    Shown ONCE per room on first contact.
    Greets the user and explains what the bot does.
    """
    # Use the local part of the email as a friendly name, e.g. "jgorle"
    greeting = f"👋 Hi {user_email.split('@')[0]}!" if user_email else "👋 Hi there!"
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "TextBlock",
                "text": f"{greeting} I'm the Case Summary Bot 🤖",
                "weight": "Bolder",
                "size": "Large",
                "color": "Accent",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    "I can generate AI-powered summaries of your ServiceNow cases. "
                    "Just send me a case number (e.g. **CS0001051**) or type "
                    "**summarize CS0001051** and I'll fetch the case, build a "
                    "timeline, and summarise it for you."
                ),
                "wrap": True,
                "spacing": "Medium",
            },
            {
                "type": "TextBlock",
                "text": "👇 Click **Get Started** to open the case input form.",
                "wrap": True,
                "spacing": "Small",
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Get Started",
                "style": "positive",
                "data": {"action": "open_input_card"},
            },
        ],
    }


def _input_card(
    title: str = "🔍 Case Summary Bot",
    subtitle: str = "Enter a ServiceNow case number to generate an AI-powered summary.",
) -> Dict[str, Any]:
    """Input form asking the user for a case number."""
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Medium",
                "color": "Accent",
            },
            {
                "type": "TextBlock",
                "text": subtitle,
                "wrap": True,
                "spacing": "Small",
                "color": "Default",
            },
            {
                "type": "Input.Text",
                "id": "case_number",
                "placeholder": "e.g. CS0001051",
                "isRequired": True,
                "errorMessage": "Please enter a valid case number (e.g. CS0001051)",
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Summarize",
                "style": "positive",
                "data": {"action": "summarize_case"},
            },
            {
                "type": "Action.Submit",
                "title": "Cancel",
                "data": {"action": "exit_menu"},
            },
        ],
    }


def _working_card(case_number: str) -> Dict[str, Any]:
    """Interim card shown while the LLM is generating the summary."""
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [
            {
                "type": "TextBlock",
                "text": f"⏳ Generating summary for **{case_number}**…",
                "wrap": True,
                "weight": "Bolder",
                "color": "Accent",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Fetching the case from ServiceNow and building the timeline. "
                    "This usually takes a few seconds — the card will update automatically."
                ),
                "wrap": True,
            },
        ],
    }


def _parse_summary_sections(summary_text: str) -> list:
    """
    Split an LLM summary into (header, body) pairs.
    Recognises lines like  "Problem:"  or  "What Was Done:"  as section headers.
    The first chunk before any recognised header is the metadata line.
    """
    section_headers = {
        "Problem:", "Root Cause:", "What Was Done:",
        "Current Status:", "Next Steps:", "Overview:",
        "Actions Taken:", "Key Points:",
    }
    sections: list = []
    current_header = ""
    current_lines: list = []

    for line in summary_text.splitlines():
        stripped = line.strip()
        if stripped in section_headers:
            # Flush previous section
            if current_header or current_lines:
                sections.append((current_header, "\n".join(current_lines).strip()))
            current_header = stripped
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last section
    if current_header or current_lines:
        sections.append((current_header, "\n".join(current_lines).strip()))

    return sections


def _summary_card(case_number: str, summary_text: str) -> Dict[str, Any]:
    """Final card showing the AI-generated summary with bold section headers."""
    max_chars = 2000
    body_text = summary_text if len(summary_text) <= max_chars else summary_text[:max_chars] + "…"

    sections = _parse_summary_sections(body_text)

    body_blocks: list = [
        {
            "type": "TextBlock",
            "text": f"📋 Summary — {case_number}",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Accent",
        },
    ]

    for header, content in sections:
        if not header and content:
            # Metadata line (no header) — render in a highlighted container
            body_blocks.append({
                "type": "ColumnSet",
                "spacing": "Medium",
                "columns": [
                    {
                        "type": "Column",
                        "width": "4px",
                        "items": [{
                            "type": "Image",
                            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e4/Infobox_info_icon.svg/1px-Infobox_info_icon.svg.png",
                            "width": "4px",
                            "backgroundColor": "#0078D4",
                        }],
                        "style": "accent",
                        "bleed": True,
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [{
                            "type": "TextBlock",
                            "text": f"**{content}**",
                            "wrap": True,
                            "color": "Accent",
                            "size": "Small",
                        }],
                    },
                ],
            })
        elif header:
            # Section header — bold, accent colour
            body_blocks.append({
                "type": "TextBlock",
                "text": f"**{header}**",
                "weight": "Bolder",
                "color": "Accent",
                "wrap": True,
                "spacing": "Medium",
            })
            if content:
                body_blocks.append({
                    "type": "TextBlock",
                    "text": content,
                    "wrap": True,
                    "spacing": "Small",
                })

    # Separator line before actions
    body_blocks.append({
        "type": "TextBlock",
        "text": " ",
        "spacing": "Small",
    })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": body_blocks,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Summarize another case",
                "style": "positive",
                "data": {"action": "open_input_card"},
            },
            {
                "type": "Action.Submit",
                "title": "Close",
                "data": {"action": "close_summary"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# STEP 3 — ServiceNow + LLM pipeline
# ---------------------------------------------------------------------------

def extract_case_number(text: str) -> Optional[str]:
    """Pull the first CS or TASK-prefixed number out of a string."""
    if not text:
        return None
    match = re.search(r"\b(?:CS|TASK)\d+\b", text.upper())
    return match.group() if match else None


def is_bare_case_number(text: str) -> bool:
    """True if the entire message is just a case number, e.g. 'CS0001051' or 'TASK0020148'."""
    return bool(text and re.fullmatch(r"(?:CS|TASK)\d+", text.strip(), re.IGNORECASE))


def get_summary(case_number: str) -> Dict[str, Any]:
    """
    Full ServiceNow → LLM pipeline:
      3a. Fetch case record by number
      3b. Resolve sys_id (may be nested dict in some SN instances)
      3c. Fetch journal entries + email history
      3d. Build a chronological timeline
      3e. Call LLM summarizer
    """
    # -- 3a. Fetch case record ------------------------------------------------
    print(f"  [STEP 3a / get_summary] 🔍 Fetching case record for {case_number}")
    case_record = get_case_by_number(case_number)
    if not case_record:
        print(f"  [STEP 3a / get_summary] ❌ Case not found in ServiceNow")
        return {"case_number": case_number, "summary": "❌ Case not found in ServiceNow."}

    # -- 3b. Resolve sys_id ---------------------------------------------------
    raw_sys_id = case_record.get("sys_id")
    if isinstance(raw_sys_id, dict):
        raw_sys_id = raw_sys_id.get("value") or raw_sys_id.get("display_value")

    if not raw_sys_id:
        print(f"  [STEP 3b / get_summary] ❌ Case record has no sys_id")
        return {"case_number": case_number, "summary": "❌ Case record is missing a sys_id."}

    print(f"  [STEP 3b / get_summary] ✅ sys_id={raw_sys_id}")

    # -- 3c. Fetch journal + email history ------------------------------------
    print(f"  [STEP 3c / get_summary] 🔍 Fetching journal entries and emails")
    journal_entries = get_case_journal_entries(raw_sys_id)
    email_entries   = get_case_emails(raw_sys_id)
    print(f"  [STEP 3c / get_summary] ✅ journal={len(journal_entries)}  emails={len(email_entries)}")

    # -- 3d. Build timeline ---------------------------------------------------
    print(f"  [STEP 3d / get_summary] ⏩ Building timeline")
    timeline = build_timeline(journal_entries, email_entries)
    print(f"  [STEP 3d / get_summary] ✅ {len(timeline)} events in timeline")

    # -- 3e. Call LLM ---------------------------------------------------------
    print(f"  [STEP 3e / get_summary] ⏩ Calling LLM summarizer")
    llm_summary = summarize_case_with_llm(case_record, timeline)
    print(f"  [STEP 3e / get_summary] ✅ LLM returned {len(llm_summary)} chars")

    return {"case_number": case_number, "summary": llm_summary}


def format_reply(result: Dict[str, Any]) -> str:
    """Convert get_summary result dict into a plain display string."""
    case_number = result.get("case_number", "")
    summary     = (result.get("summary") or "").strip()
    if not summary or summary.startswith("❌"):
        return f"Could not generate summary for {case_number}.\n\n{summary}"
    return summary


def _summarize_and_flip(room_id: str, case_number: str, card_message_id: Optional[str]) -> None:
    """
    Background thread — runs the full ServiceNow → LLM pipeline then
    replaces the working-card with the summary-card.
    Always sends something back to the user, even on failure.
    """
    print(f"\n  [THREAD / _summarize_and_flip] ⏩ START  room={room_id}  case={case_number}  card_id={card_message_id}")
    try:
        result  = get_summary(case_number)
        summary = format_reply(result)
        sum_card = _summary_card(case_number, summary)

        print(f"  [THREAD / _summarize_and_flip] ⏩ Delivering summary card")
        if card_message_id:
            replace_card(card_message_id, sum_card, fallback_text=f"Summary — {case_number}", room_id=room_id)
        else:
            send_card(room_id, sum_card, fallback_text=f"Summary — {case_number}")

        print(f"  [THREAD / _summarize_and_flip] ✅ DONE — {case_number}")

    except Exception as exc:
        import traceback
        print(f"  [THREAD / _summarize_and_flip] ❌ Exception for {case_number}: {repr(exc)}")
        traceback.print_exc()
        # Never fail silently — always tell the user something went wrong
        send_text(room_id, f"❌ Something went wrong generating the summary for {case_number}. Please try again.")


# ---------------------------------------------------------------------------
# STEP 4 — Noise / echo filters
# ---------------------------------------------------------------------------

# Phrases that Webex echoes back as the plain-text fallback of cards the bot
# sent. We must ignore these or the bot will enter an infinite loop.
BOT_FALLBACK_PHRASES: Set[str] = {
    "case summary bot",
    "case summary bot – enter a case number to summarize",
    "generating summary…",
    "generating summary...",
    "summary closed",
    "summarize another case?",
    "summary —",
    "welcome to case summary bot!",
}


def _is_noise(text: str) -> bool:
    """Return True if this message is an echo of something the bot itself sent."""
    if re.search(r"Summary for (?:CS|TASK)\d+", text, re.IGNORECASE):
        return True
    if text.lower().strip() in BOT_FALLBACK_PHRASES:
        return True
    return False


# ---------------------------------------------------------------------------
# STEP 5 — Welcome + message routing
# ---------------------------------------------------------------------------

def _maybe_send_welcome(room_id: str, user_email: str) -> None:
    """
    Send a welcome card the very first time we see a room.
    Uses the in-memory welcomed_rooms set to fire only once per session.
    On server restart the set is empty, so users get a re-welcome — that's fine.
    """
    if room_id in welcomed_rooms:
        print(f"  [STEP 5 / _maybe_send_welcome] ⏩ Room already welcomed — skipping")
        return

    print(f"  [STEP 5 / _maybe_send_welcome] 🆕 First contact in room={room_id} user={user_email!r} — sending welcome card")
    welcomed_rooms.add(room_id)
    send_card(
        room_id,
        _welcome_card(user_email),
        fallback_text="Welcome to Case Summary Bot!",
    )


def _show_input_card(
    room_id: str,
    card_message_id: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
) -> None:
    """Send or replace-in-place with the input card. Avoids duplication across callers."""
    kwargs: Dict[str, str] = {}
    if title:
        kwargs["title"] = title
    if subtitle:
        kwargs["subtitle"] = subtitle
    card = _input_card(**kwargs)
    if card_message_id:
        replace_card(card_message_id, card, fallback_text="Case Summary Bot", room_id=room_id)
    else:
        send_card(room_id, card, fallback_text="Case Summary Bot")


def _route_message(room_id: str, text: str, user_email: str = "") -> dict:
    """
    Core routing logic for an incoming text message.

    Priority order:
      5a. Check / send welcome if first contact
      5b. exit / quit / close  → goodbye text
      5c. bare case number     → working card + background summary thread
      5d. "summarize CS..."    → same as 5c
      5e. anything else        → show the input form
    """
    text_stripped = text.strip()
    text_lower    = text_stripped.lower()
    print(f"  [STEP 5 / _route_message] ⏩ Routing text_lower={repr(text_lower[:80])}")

    # ── 5a. Welcome (first contact) ───────────────────────────────────────
    _maybe_send_welcome(room_id, user_email)

    # ── 5b. Exit commands ──────────────────────────────────────────────────
    if text_lower in {"exit", "quit", "close"}:
        print(f"  [STEP 5b / _route_message] ⏩ Exit command")
        send_text(room_id, "Closed ✅ Message me anytime or enter a case number to start a new summary.")
        return {"status": "ok", "reason": "Exited via text command"}

    # ── 5c. Bare case number (e.g. "CS0001051") ───────────────────────────
    if is_bare_case_number(text_stripped):
        case_number = text_stripped.upper()
        print(f"  [STEP 5c / _route_message] ⏩ Bare case number: {case_number}")
        card_id = send_card(room_id, _working_card(case_number), fallback_text="Generating summary…")
        _invoke_summary_async(room_id, case_number, card_id)
        return {"status": "ok", "case_number": case_number}

    # ── 5d. "summarize CS..." command ─────────────────────────────────────
    if text_lower.startswith("summarize"):
        direct_case = extract_case_number(text_stripped)
        if direct_case:
            print(f"  [STEP 5d / _route_message] ⏩ 'summarize' command — case={direct_case}")
            card_id = send_card(room_id, _working_card(direct_case), fallback_text="Generating summary…")
            _invoke_summary_async(room_id, direct_case, card_id)
            return {"status": "ok", "case_number": direct_case}

    # ── 5e. Fallback — show the input form ────────────────────────────────
    print(f"  [STEP 5e / _route_message] ⏩ No pattern matched — showing input card")
    send_card(room_id, _input_card(), fallback_text="Case Summary Bot – enter a case number to summarize")
    return {"status": "ok", "reason": "Input card shown"}


# ---------------------------------------------------------------------------
# Card-action helpers
# ---------------------------------------------------------------------------

def _parse_action(action_details: Dict[str, Any]) -> Optional[str]:
    """Extract the 'action' string from card submit payload."""
    for key in ("inputs", "data"):
        container = action_details.get(key) or {}
        if isinstance(container, dict) and container.get("action"):
            return container["action"]
    return None


def _parse_case_from_action(action_details: Dict[str, Any]) -> Optional[str]:
    """Extract and validate the case number typed into the input card."""
    inputs = action_details.get("inputs") or {}
    if not isinstance(inputs, dict):
        return None
    return extract_case_number(inputs.get("case_number", ""))


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"message": "Case Summary Bot is running ✅"}


@app.get("/debug-env")
def debug_env():
    """Quick health-check endpoint to verify config and see welcomed room count."""
    return {
        "has_webex_token": bool(WEBEX_BOT_TOKEN),
        "webex_bot_email": WEBEX_BOT_EMAIL,
        "welcomed_rooms_count": len(welcomed_rooms),
    }


# ── Webhook: incoming messages ─────────────────────────────────────────────

@app.post("/webhook/webex")
async def webex_webhook(request: Request):
    """
    Receives every new DM in the org (org-wide webhook).
    The bot can only read/reply in rooms it is ALREADY a member of.
    If someone hasn't DM'd the bot yet, message fetches will 404 and we skip.
    Users must open a DM with the bot directly to start using it.
    """
    try:
        body = await request.json()
        data = body.get("data", {})

        message_id  = data.get("id")
        room_id     = data.get("roomId")
        parent_id   = data.get("parentId")
        actor_id    = body.get("actorId", "unknown")
        # NOTE: outer personEmail is often absent in Webex org-wide webhooks
        outer_email = (data.get("personEmail") or "").lower()

        print(f"\n{'='*64}")
        print(f"[WEBHOOK /webex] ⏩ NEW EVENT")
        print(f"  message_id  = {message_id}")
        print(f"  room_id     = {room_id}")
        print(f"  actor_id    = {actor_id}")
        print(f"  outer_email = {outer_email!r}")
        print(f"  parent_id   = {parent_id!r}")

        # ── Guard 1: Required fields ──────────────────────────────────────
        if not message_id or not room_id:
            print("  ❌ SKIP: missing message_id or room_id")
            return {"status": "ignored", "reason": "Missing message_id or room_id"}

        # ── Guard 2: Skip thread replies ──────────────────────────────────
        if parent_id:
            print(f"  ❌ SKIP: thread reply (parent_id={parent_id})")
            return {"status": "ignored", "reason": "Thread reply"}

        # ── Guard 3: Skip if outer email is clearly the bot ───────────────
        # (outer_email can be empty; deeper check happens after fetch)
        if outer_email and is_bot_message(outer_email):
            print(f"  ❌ SKIP: outer email is bot ({outer_email!r})")
            return {"status": "ignored", "reason": "Bot event (outer payload)"}

        # ── STEP 1: Fetch the full message ────────────────────────────────
        print(f"  ⏩ STEP 1 — Fetching full message from Webex API…")
        message = get_webex_message(message_id)

        if not message:
            # 404 = bot is not a member of this room.
            # Fires for DMs between two humans when org-wide webhook catches them.
            print(f"  ❌ SKIP: cannot fetch message — bot not in room")
            print(f"         room_id={room_id}  actor_id={actor_id}")
            print(f"         ➡  FIX: user ({outer_email or actor_id}) must open a DM with the bot directly.")
            return {"status": "ignored", "reason": "Message not accessible — bot not in room"}

        fetched_email = (message.get("personEmail") or "").lower()
        text          = (message.get("text") or "").strip()

        print(f"  fetched_email = {fetched_email!r}")
        print(f"  text          = {repr(text[:120])}")

        # ── Guard 4: Skip bot's own messages ─────────────────────────────
        if is_bot_message(fetched_email):
            print(f"  ❌ SKIP: fetched email is a bot ({fetched_email!r})")
            return {"status": "ignored", "reason": "Bot event (fetched message)"}

        # ── Guard 5: Skip echoed card fallback text ───────────────────────
        if _is_noise(text):
            print(f"  ❌ SKIP: noise/echo ({repr(text[:60])})")
            return {"status": "ignored", "reason": "Noise / bot echo text"}

        # ── STEP 5: Route the message ─────────────────────────────────────
        print(f"  ✅ All guards passed — routing message from {fetched_email!r}")
        return _route_message(room_id, text, user_email=fetched_email)

    except Exception as exc:
        import traceback
        print(f"[WEBHOOK /webex] ❌ EXCEPTION: {repr(exc)}")
        traceback.print_exc()
        return {"status": "error", "detail": str(exc)}


# ── Webhook: Adaptive Card button actions ─────────────────────────────────

@app.post("/webhook/webex/card-action")
async def webex_card_action_webhook(request: Request):
    """
    Fires when a user clicks a button or submits a form on an Adaptive Card.

    action names dispatched here:
      open_input_card  → show the case-number input form
      summarize_case   → validate input, kick off summary thread
      close_summary    → return to input form
      exit_menu        → send a goodbye plain-text message
    """
    try:
        body = await request.json()
        data = body.get("data", {})

        action_id       = data.get("id")
        room_id         = data.get("roomId")
        person_email    = (data.get("personEmail") or "").lower()
        card_message_id = data.get("messageId")

        print(f"\n{'='*64}")
        print(f"[WEBHOOK /card-action] ⏩ NEW CARD ACTION")
        print(f"  action_id       = {action_id}")
        print(f"  room_id         = {room_id}")
        print(f"  person_email    = {person_email!r}")
        print(f"  card_message_id = {card_message_id}")

        # ── Guard 1: Required fields ──────────────────────────────────────
        if not action_id or not room_id:
            print("  ❌ SKIP: missing action_id or room_id")
            return {"status": "ignored", "reason": "Missing action_id or room_id"}

        # ── Guard 2: Skip bot actions ─────────────────────────────────────
        if is_bot_message(person_email):
            print(f"  ❌ SKIP: action from bot ({person_email!r})")
            return {"status": "ignored", "reason": "Bot action event"}

        # ── STEP 1: Fetch action details (card inputs) ────────────────────
        print(f"  ⏩ STEP 1 — Fetching card action details…")
        action_details = get_attachment_action(action_id)
        if not action_details:
            print("  ❌ SKIP: could not fetch action details (deleted or 404)")
            return {"status": "ignored", "reason": "Could not fetch action details"}

        action_name = _parse_action(action_details)
        print(f"  ⏩ action_name = {action_name!r}")

        # ── Dispatch ──────────────────────────────────────────────────────

        # User clicked "Get Started" on the welcome card, or "Summarize another"
        if action_name == "open_input_card":
            print(f"  ⏩ Dispatch: open_input_card — showing input form")
            _show_input_card(room_id, card_message_id)
            return {"status": "ok", "reason": "Input card shown"}

        # User clicked "Cancel" on the input card
        if action_name == "exit_menu":
            print(f"  ⏩ Dispatch: exit_menu — sending goodbye text")
            send_text(room_id, "Closed ✅ Enter a case number anytime to generate a new summary.")
            return {"status": "ok", "reason": "Exited (exit_menu)"}

        # User clicked "Close" on the summary card — go back to input form
        if action_name == "close_summary":
            print(f"  ⏩ Dispatch: close_summary — returning to input card")
            _show_input_card(room_id, card_message_id)
            return {"status": "ok", "reason": "Returned to input card (close_summary)"}

        # User submitted the input form
        if action_name == "summarize_case":
            case_number = _parse_case_from_action(action_details)
            print(f"  ⏩ Dispatch: summarize_case — case_number={case_number!r}")

            if not case_number:
                # Input was blank or not a valid CS number
                print(f"  ❌ Invalid or missing case number — re-showing input with error")
                _show_input_card(
                    room_id,
                    card_message_id,
                    title="⚠️ Invalid case number",
                    subtitle="Please enter a valid case number like CS0001051.",
                )
                return {"status": "ok", "reason": "Invalid case number — re-showing input"}

            # Immediately flip to working card so user gets instant feedback
            print(f"  ⏩ Showing working card for {case_number}")
            working_card_id = card_message_id
            if card_message_id:
                replace_card(card_message_id, _working_card(case_number), fallback_text="Generating summary…", room_id=room_id)
            else:
                working_card_id = send_card(room_id, _working_card(case_number), fallback_text="Generating summary…")

            # Fire async Lambda invocation — returns immediately
            print(f"  ⏩ Dispatching async summary for {case_number}")
            _invoke_summary_async(room_id, case_number, working_card_id)
            return {"status": "ok", "case_number": case_number}

        # Unknown action — log and ignore
        print(f"  ⚠️  Unknown action_name: {action_name!r} — ignoring")
        return {"status": "ignored", "reason": f"Unknown action: {action_name}"}

    except Exception as exc:
        import traceback
        print(f"[WEBHOOK /card-action] ❌ EXCEPTION: {repr(exc)}")
        traceback.print_exc()
        return {"status": "error", "detail": str(exc)}