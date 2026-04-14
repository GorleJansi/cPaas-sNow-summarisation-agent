# Case Summary Bot — Complete Reference Guide

> **Author:** Jansi Gorle · Technical Consulting Engineer · CX
> **Date:** April 2026 · Proof of Concept
> **GitHub:** [github.com/GorleJansi/cPaas-sNow-summarisation-agent](https://github.com/GorleJansi/cPaas-sNow-summarisation-agent)

---

## 1. What This Bot Does

An engineer sends a ServiceNow case number (e.g. `CS0001028`) to the **Case Summary Bot** in Webex.
The bot fetches the case from ServiceNow, builds a timeline from journal entries + emails,
calls Cisco CIRCUIT LLM to generate a concise summary, and delivers it back as an Adaptive Card — all in ~5-8 seconds.

---

## 2. Architecture Diagram

```
┌──────────────────┐
│   Engineer in     │
│   Webex (DM)      │
│   types CS0001028 │
└────────┬─────────┘
         │ Webex fires webhook (HTTP POST)
         ▼
┌──────────────────────────────────────────┐
│  AWS API Gateway (HTTP API v2)           │
│  https://fe4puvvg5j.execute-api.         │
│         us-east-1.amazonaws.com          │
│                                          │
│  Routes:                                 │
│    POST /webhook/webex       → Lambda    │
│    POST /webhook/webex/card-action → Lambda │
└────────┬─────────────────────────────────┘
         │ Translates HTTP → Lambda event
         ▼
┌──────────────────────────────────────────┐
│  AWS Lambda                              │
│  Function: cPaas-sNow-summarisation-agent│
│  Runtime: Python 3.13 | Memory: 256 MB  │
│  Timeout: 120s | Handler: lambda_handler.handler │
│                                          │
│  1. Receives webhook event               │
│  2. Validates (not a bot msg, not noise) │
│  3. Sends "Generating..." working card   │
│  4. Invokes ITSELF async (Event mode)    │
│     └─ Returns 200 to Webex immediately  │
│                                          │
│  [Async invocation — separate execution] │
│  5. Fetches case from ServiceNow         │
│  6. Fetches journal entries + emails     │
│  7. Builds chronological timeline        │
│  8. Calls CIRCUIT LLM for summary       │
│  9. Replaces working card with summary   │
└──────┬───────────────┬───────────────────┘
       │               │
       ▼               ▼
┌──────────────┐ ┌─────────────────────┐
│ ServiceNow   │ │ Cisco CIRCUIT LLM   │
│ CSM REST API │ │ (via chat-ai.cisco) │
│              │ │                     │
│ Instance:    │ │ Token: id.cisco.com │
│ dev380388    │ │ Model: gpt-5-nano   │
└──────────────┘ └─────────────────────┘
```

### Why the Async Self-Invocation?

API Gateway has a **30-second timeout**. The full pipeline (ServiceNow fetch + LLM call) can take 10-20s.
So the Lambda splits into two executions:
- **Execution 1 (HTTP):** Receives webhook → sends working card → invokes itself async → returns 200 instantly (~1-2s)
- **Execution 2 (Async):** Runs the heavy pipeline with a full 120s timeout → replaces working card with summary

---

## 3. Complete Workflow (Step by Step)

```
STEP 1: User sends "CS0001028" in Webex DM
   │
STEP 2: Webex platform fires HTTP POST to:
   │    https://fe4puvvg5j.execute-api.us-east-1.amazonaws.com/webhook/webex
   │    Body: { data: { id: "<message_id>", roomId: "<room_id>" } }
   │
STEP 3: Lambda receives event → Mangum translates to FastAPI request
   │    File: lambda_handler.py → handler() → _mangum(event, context)
   │
STEP 4: FastAPI route @app.post("/webhook/webex") fires
   │    File: app.py → webex_webhook()
   │    - Fetches full message from Webex API (GET /v1/messages/{id})
   │    - Checks: is it a bot message? is it noise/echo? → skip if yes
   │    - Extracts text, identifies case number
   │
STEP 5: _route_message() determines what to do
   │    - First contact? → send welcome card
   │    - Bare case number (CS0001028)? → working card + async invoke
   │    - "summarize CS0001028"? → same
   │    - Anything else? → show input card form
   │
STEP 6: send_card() sends the "⏳ Generating summary..." working card
   │    POST https://webexapis.com/v1/messages (with Adaptive Card attachment)
   │
STEP 7: _invoke_summary_async() fires Lambda async self-invocation
   │    boto3.client("lambda").invoke(FunctionName=self, InvocationType="Event")
   │    Payload: { "_async_summary": true, room_id, case_number, card_message_id }
   │    → HTTP 200 returned to Webex immediately
   │
STEP 8: Lambda fires again with the async event
   │    handler() sees "_async_summary" → calls _summarize_and_flip()
   │
STEP 9: _summarize_and_flip() runs the pipeline:
   │    a. get_case_by_number(case_number) → ServiceNow REST API
   │    b. get_case_journal_entries(sys_id) → comments + work notes
   │    c. get_case_emails(sys_id) → email threads
   │    d. build_timeline(journal, emails) → sorted chronological list
   │    e. summarize_case_with_llm(case_data, timeline) → CIRCUIT LLM
   │
STEP 10: replace_card() swaps the working card with the summary card
         PATCH https://webexapis.com/v1/messages/{card_id}
```

---

## 4. Where Everything is Registered / Hosted

### 4.1 Webex Bot Registration

| Item | Value |
|------|-------|
| **Portal** | [developer.webex.com/my-apps](https://developer.webex.com/my-apps) |
| **Bot Name** | Case Summary Bot |
| **Bot Email** | `Case_Summary_Bot@webex.bot` |
| **Bot ID** | `Y2lzY29zcGFyazovL3VzL0FQUExJQ0FUSU9OLzk2YWM3ZGJlLWRkZjctNDVkMC1hYTNjLTIwZmM1ZGU0NmU5Mw` |
| **Token** | Stored in Lambda env var `WEBEX_BOT_TOKEN` (non-expiring, 100-year token) |

### 4.2 Webex Webhooks

Registered via Webex REST API (`POST https://webexapis.com/v1/webhooks`) using the bot token.
You can view/manage them at [developer.webex.com/docs/webhooks](https://developer.webex.com/docs/webhooks).

| Webhook | Target URL | Resource | Event |
|---------|-----------|----------|-------|
| **Messages** | `https://fe4puvvg5j.execute-api.us-east-1.amazonaws.com/webhook/webex` | `messages` | `created` |
| **Card Actions** | `https://fe4puvvg5j.execute-api.us-east-1.amazonaws.com/webhook/webex/card-action` | `attachmentActions` | `created` |

To list webhooks:
```bash
curl -s -H "Authorization: Bearer $WEBEX_BOT_TOKEN" https://webexapis.com/v1/webhooks | python3 -m json.tool
```

### 4.3 AWS Hosting

| Item | Value |
|------|-------|
| **AWS Account** | 980596553160 (AWS_CustomerMonitoring_US1) |
| **Region** | us-east-1 |
| **Console Sign-in** | [awscustomermonitoringus1.signin.aws.amazon.com/console](https://awscustomermonitoringus1.signin.aws.amazon.com/console) |
| **Lambda Function** | `cPaas-sNow-summarisation-agent` |
| **Lambda Console** | [Lambda Console Direct Link](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/cPaas-sNow-summarisation-agent) |
| **API Gateway** | `https://fe4puvvg5j.execute-api.us-east-1.amazonaws.com` |
| **IAM User** | `summary_agent` (for CLI deployments) |
| **Handler** | `lambda_handler.handler` |
| **Runtime** | Python 3.13 |
| **Memory** | 256 MB |
| **Timeout** | 120 seconds |

### 4.4 ServiceNow Instance

| Item | Value |
|------|-------|
| **Portal** | [developer.servicenow.com](https://developer.servicenow.com) |
| **Instance** | `dev380388.service-now.com` |
| **Version** | Australia (latest) |
| **Username** | `admin` |
| **Type** | Personal Developer Instance (PDI) — hibernates after inactivity |
| **CSM Plugin** | Customer Service Management (must be activated) |
| **Tables Used** | `sn_customerservice_case`, `sys_journal_field`, `sys_email` |

> ⚠️ **PDI Hibernation:** The instance sleeps after inactivity. Wake it at [developer.servicenow.com](https://developer.servicenow.com/dev.do#!/home?wu=true) before testing.

### 4.5 CIRCUIT LLM (Cisco Internal AI Gateway)

| Item | Value |
|------|-------|
| **Registration Portal** | [EGAI Portal](https://egai.cisco.com) (Cisco internal) |
| **Token Endpoint** | `https://id.cisco.com/oauth2/default/v1/token` |
| **Chat API Base** | `https://chat-ai.cisco.com/openai/deployments` |
| **Model** | `gpt-5-nano` (Free Fair Use tier) |
| **App Key** | `egai-prd-cx-123212180-summarize-1774871716656` |
| **Department ID** | 123212180 (CX) |
| **Auth Flow** | OAuth2 client_credentials → Basic auth with Client ID + Secret |
| **Data Classification** | Cisco Confidential |

### 4.6 GitHub Repository

| Item | Value |
|------|-------|
| **URL** | [github.com/GorleJansi/cPaas-sNow-summarisation-agent](https://github.com/GorleJansi/cPaas-sNow-summarisation-agent) |
| **Branch** | `main` |

---

## 5. Project File Structure

```
cPaas-sNow-summarisation-agent/
│
├── lambda_handler.py      # Lambda entrypoint (43 lines)
├── app.py                 # FastAPI app — webhooks, cards, routing (940 lines)
├── config.py              # Environment variable loader (32 lines)
├── servicenow_client.py   # ServiceNow REST API client (80 lines)
├── summarizer.py          # CIRCUIT LLM integration (222 lines)
├── formatter.py           # Timeline builder (79 lines)
│
├── .env                   # Real credentials (git-ignored, never commit)
├── .env.example           # Template with placeholder values
├── .gitignore             # Git ignore rules
├── requirements.txt       # Python dependencies (16 packages)
├── deploy.sh              # One-command build & deploy script
├── README.md              # Project overview
├── REFERENCE.md           # This file — complete reference guide
│
└── vendor/                # Third-party packages (git-ignored, auto-installed)
```

---

## 6. Code Functions — What Each Does & How They Call Each Other

### 6.1 `lambda_handler.py` — Entry Point

```
handler(event, context)
  │
  ├─ if event["_async_summary"] → _summarize_and_flip(room_id, case_number, card_id)
  │                                 (imported from app.py)
  │
  └─ else → _mangum(event, context)
              (Mangum adapter translates API Gateway event → FastAPI ASGI request)
```

| Function | Purpose |
|----------|---------|
| `handler(event, context)` | Lambda entrypoint. Routes between HTTP events (→ Mangum/FastAPI) and async summary events (→ direct pipeline call). |

### 6.2 `app.py` — Core Application (940 lines)

This is the main file. Here's every important function and how they connect:

#### Webex API Helpers (low-level)

| Function | Purpose | Calls |
|----------|---------|-------|
| `is_bot_message(email)` | Returns True if email belongs to a bot (prevents infinite loops) | — |
| `_headers()` | Returns auth headers with bot token | — |
| `_request(method, url)` | HTTP wrapper with retry logic (3 retries, handles 404/405) | `requests.request()` |
| `get_webex_message(message_id)` | Fetches a single message by ID from Webex API | `_request("GET", ...)` |
| `get_attachment_action(action_id)` | Fetches Adaptive Card submit data | `_request("GET", ...)` |
| `send_text(room_id, text)` | Sends a plain text message to a Webex room | `_request("POST", ...)` |
| `send_card(room_id, card)` | Sends an Adaptive Card to a Webex room, returns message ID | `_request("POST", ...)` |
| `replace_card(message_id, card)` | Replaces (PATCHes) an existing card in-place | `_request("PATCH", ...)` → fallback: `send_card()` |

#### Adaptive Card Templates

| Function | Returns | Used When |
|----------|---------|-----------|
| `_welcome_card(user_email)` | Welcome greeting card with "Get Started" button | First-time contact in a room |
| `_input_card(title, subtitle)` | Form card with case number text input + "Summarize" button | User needs to enter a case number |
| `_working_card(case_number)` | "⏳ Generating summary..." interim card | While LLM is processing |
| `_summary_card(case_number, summary_text)` | Final summary card with sections (Problem, Root Cause, etc.) | After LLM returns |
| `_parse_summary_sections(text)` | Splits LLM output into (header, body) pairs | Called by `_summary_card()` |

#### Pipeline Functions

| Function | Purpose | Calls |
|----------|---------|-------|
| `extract_case_number(text)` | Regex: pulls first `CS` or `TASK` number from text | — |
| `is_bare_case_number(text)` | True if entire message is just a case number | — |
| `get_summary(case_number)` | **Full pipeline**: SN fetch → journal → emails → timeline → LLM | `get_case_by_number()` → `get_case_journal_entries()` → `get_case_emails()` → `build_timeline()` → `summarize_case_with_llm()` |
| `format_reply(result)` | Converts pipeline result dict to display string | — |
| `_summarize_and_flip(room_id, case_number, card_id)` | Runs `get_summary()` then `replace_card()` with result. Always sends something (even on error). | `get_summary()` → `_summary_card()` → `replace_card()` |
| `_invoke_summary_async(room_id, case_number, card_id)` | Fire-and-forget: invokes THIS Lambda asynchronously via boto3 | `boto3.client("lambda").invoke(InvocationType="Event")` |

#### Routing & Webhook Handlers

| Function | Purpose | Calls |
|----------|---------|-------|
| `_is_noise(text)` | Detects echoed bot fallback text (prevents infinite loops) | — |
| `_maybe_send_welcome(room_id, email)` | Sends welcome card once per room (tracked in memory set) | `send_card()` + `_welcome_card()` |
| `_show_input_card(room_id, ...)` | Sends or replaces with the input form | `send_card()` or `replace_card()` |
| `_route_message(room_id, text, email)` | **Main router**: welcome → exit → bare case → summarize cmd → fallback input card | `_maybe_send_welcome()` → `send_card()` → `_invoke_summary_async()` |
| `_parse_action(action_details)` | Extracts action name from card submit payload | — |
| `_parse_case_from_action(action_details)` | Extracts case number from card form input | `extract_case_number()` |

#### FastAPI Route Handlers

| Route | Handler | What It Does |
|-------|---------|-------------|
| `GET /` | `root()` | Health check — returns `{"message": "Case Summary Bot is running ✅"}` |
| `GET /debug-env` | `debug_env()` | Shows config state (has token? bot email? welcomed rooms count) |
| `POST /webhook/webex` | `webex_webhook()` | Receives Webex message events. Guards: skip bots, skip noise, skip thread replies. Then calls `_route_message()`. |
| `POST /webhook/webex/card-action` | `webex_card_action_webhook()` | Receives card button clicks. Dispatches: `open_input_card`, `summarize_case`, `close_summary`, `exit_menu`. |

#### Complete Call Graph for a Summary Request

```
webex_webhook()                          ← Webex fires HTTP POST
  ├─ get_webex_message(id)               ← Fetch full message from Webex API
  ├─ is_bot_message(email)               ← Guard: skip bot messages
  ├─ _is_noise(text)                     ← Guard: skip echoed fallback text
  └─ _route_message(room_id, text)       ← Main routing
       ├─ _maybe_send_welcome()          ← First contact? → welcome card
       ├─ send_card(_working_card())     ← Show "Generating..." card
       └─ _invoke_summary_async()        ← Fire-and-forget async Lambda call
            │
            ▼ [New Lambda execution]
handler(event)                           ← Lambda entrypoint (async mode)
  └─ _summarize_and_flip()
       └─ get_summary(case_number)
            ├─ get_case_by_number()      ← ServiceNow: fetch case metadata
            ├─ get_case_journal_entries()← ServiceNow: fetch comments + work notes
            ├─ get_case_emails()         ← ServiceNow: fetch email threads
            ├─ build_timeline()          ← Merge & sort chronologically
            └─ summarize_case_with_llm() ← CIRCUIT LLM call
                 ├─ build_prompt()       ← Construct the LLM prompt
                 ├─ get_access_token()   ← OAuth2 token from id.cisco.com
                 └─ call_circuit_llm()   ← POST to chat-ai.cisco.com
       └─ replace_card(_summary_card()) ← Swap working card → summary card
```

### 6.3 `servicenow_client.py` — ServiceNow REST API Client

| Function | API Call | Returns |
|----------|----------|---------|
| `get_case_by_number(case_number)` | `GET /api/now/table/sn_customerservice_case?number=CS0001028` | Case record dict (number, description, priority, state, etc.) |
| `get_case_journal_entries(sys_id)` | `GET /api/now/table/sys_journal_field?element_id=<sys_id>` | List of comments + work notes (with fallback to `documentkey` query) |
| `get_case_emails(sys_id)` | `GET /api/now/table/sys_email?instance=<sys_id>` | List of email threads on the case |

All use **Basic Auth** (`admin:password`) and `Accept: application/json`.

### 6.4 `summarizer.py` — CIRCUIT LLM Integration

| Function | Purpose |
|----------|---------|
| `build_prompt(case_data, timeline)` | Constructs the LLM prompt with case metadata + numbered timeline. Includes strict rules: no PII, no hallucination, deduplicate, under 200 words. |
| `get_access_token()` | OAuth2 `client_credentials` flow → `POST https://id.cisco.com/oauth2/default/v1/token` with Basic auth (client_id:secret). Returns bearer token. |
| `call_circuit_llm(prompt)` | `POST https://chat-ai.cisco.com/openai/deployments/gpt-5-nano/chat/completions` with system prompt + user prompt. Parses `choices[0].message.content`. |
| `_get_display_value(case_data, field)` | Helper: extracts display_value from ServiceNow's nested `{value, display_value}` format. |
| `_prepend_case_context(summary, case_data)` | Prepends metadata line (Priority, State, Group, Updated) to the LLM summary. |
| `summarize_case_with_llm(case_data, timeline)` | **Main entry point**: `build_prompt()` → `call_circuit_llm()` → `_prepend_case_context()`. Returns final summary string. |

### 6.5 `formatter.py` — Timeline Builder

| Function | Purpose |
|----------|---------|
| `clean_text(text)` | Removes newlines, collapses whitespace |
| `to_iso(ts)` | Converts ServiceNow timestamp (`2026-04-14 12:00:00`) to ISO 8601 |
| `map_speaker(element)` | Maps `comments` → `customer`, `work_notes` → `support_engineer` |
| `map_type(element)` | Maps `comments` → `comment`, `work_notes` → `work_note` |
| `build_timeline(journal, emails)` | Merges journal entries + emails into a single chronologically sorted list. Each item has: `type`, `source`, `speaker`, `timestamp`, `text`. |

### 6.6 `config.py` — Environment Variable Loader

Loads all env vars via `python-dotenv`. Adds `vendor/` to `sys.path` for bundled dependencies.

| Variable | Used By |
|----------|---------|
| `SERVICENOW_INSTANCE`, `USERNAME`, `PASSWORD` | `servicenow_client.py` |
| `WEBEX_BOT_TOKEN`, `WEBEX_BOT_EMAIL` | `app.py` |
| `CIRCUIT_CLIENT_ID`, `CLIENT_SECRET`, `APP_KEY`, `MODEL` | `summarizer.py` |
| `CIRCUIT_TOKEN_URL`, `CIRCUIT_CHAT_BASE_URL` | `summarizer.py` (with defaults) |

---

## 7. Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Runtime | Python 3.13 on AWS Lambda | Serverless execution |
| Web Framework | FastAPI + Mangum | HTTP handling + Lambda ASGI adapter |
| API Gateway | AWS HTTP API (v2) | Webhook receiver |
| Chat Platform | Webex Adaptive Cards v1.2 | Rich interactive UI in Webex |
| Ticketing | ServiceNow CSM REST API | Case data, journals, emails |
| AI / LLM | Cisco CIRCUIT (gpt-5-nano) | Summarization |
| Auth | Cisco ID OAuth2 (client_credentials) | CIRCUIT API token |
| Source Control | GitHub | Version control |

---

## 8. How to Deploy

```bash
# One-command deploy (from project root):
pip3 install -r requirements.txt  # only needed once locally

# Build & deploy to Lambda:
bash deploy.sh
# (or: sed 's/^pip /pip3 /' deploy.sh | bash  — if pip3 is needed)
```

The deploy script:
1. Installs dependencies for Linux x86_64 into `/tmp/lambda_linux_build/vendor/`
2. Copies the 6 source files into the build dir
3. Zips everything into `/tmp/lambda_deploy.zip`
4. Uploads to Lambda via `aws lambda update-function-code`

---

## 9. Environment Variables

All stored as Lambda environment variables + local `.env` file.

| Variable | Example | Required |
|----------|---------|----------|
| `SERVICENOW_INSTANCE` | `dev380388.service-now.com` | ✅ |
| `SERVICENOW_USERNAME` | `admin` | ✅ |
| `SERVICENOW_PASSWORD` | (see .env) | ✅ |
| `WEBEX_BOT_TOKEN` | (non-expiring bot token) | ✅ |
| `WEBEX_BOT_EMAIL` | `Case_Summary_Bot@webex.bot` | ✅ |
| `CIRCUIT_CLIENT_ID` | (Okta client ID) | ✅ |
| `CIRCUIT_CLIENT_SECRET` | (Okta client secret) | ✅ |
| `CIRCUIT_APP_KEY` | (EGAI app key) | ✅ |
| `CIRCUIT_MODEL` | `gpt-5-nano` | Optional (default: gpt-4o-mini) |
| `CIRCUIT_TOKEN_URL` | `https://id.cisco.com/oauth2/default/v1/token` | Optional |
| `CIRCUIT_CHAT_BASE_URL` | `https://chat-ai.cisco.com/openai/deployments` | Optional |

---

## 10. Adaptive Cards — User Experience Flow

```
┌─────────────────────────────────────┐
│  👋 Hi jgorle! I'm the Case        │  ← WELCOME CARD
│  Summary Bot 🤖                     │     (sent once per room)
│                                     │
│  I can generate AI-powered          │
│  summaries of your ServiceNow       │
│  cases...                           │
│                                     │
│  [Get Started]                      │
└─────────────────────────────────────┘
                │ click
                ▼
┌─────────────────────────────────────┐
│  🔍 Case Summary Bot               │  ← INPUT CARD
│  Enter a ServiceNow case number     │
│  ┌─────────────────────────────┐    │
│  │ CS0001028                   │    │
│  └─────────────────────────────┘    │
│  [Summarize]  [Cancel]              │
└─────────────────────────────────────┘
                │ click Summarize
                ▼
┌─────────────────────────────────────┐
│  ⏳ Generating summary for          │  ← WORKING CARD
│  CS0001028...                       │     (shown instantly, <2s)
│                                     │
│  Fetching the case from ServiceNow  │
│  and building the timeline.         │
└─────────────────────────────────────┘
                │ replaced in-place (~5-8s)
                ▼
┌─────────────────────────────────────┐
│  📋 Summary — CS0001028            │  ← SUMMARY CARD
│  CS0001028 — Priority: 1-Critical  │
│  | State: New                       │
│                                     │
│  Problem:                           │
│  Users see a 404 on the Webex       │
│  Connect login page...              │
│                                     │
│  Root Cause:                        │
│  CDN edge node serving stale config │
│                                     │
│  What Was Done:                     │
│  - Analyzed HAR data                │
│  - Forced CDN cache purge           │
│                                     │
│  Current Status:                    │
│  Issue resolved, monitoring...      │
│                                     │
│  [Summarize another case]  [Close]  │
└─────────────────────────────────────┘
```

---

## 11. Useful Commands

```bash
# Check bot identity
curl -s -H "Authorization: Bearer $WEBEX_BOT_TOKEN" https://webexapis.com/v1/people/me | python3 -m json.tool

# List webhooks
curl -s -H "Authorization: Bearer $WEBEX_BOT_TOKEN" https://webexapis.com/v1/webhooks | python3 -m json.tool

# Check Lambda logs (last 5 min)
aws logs tail /aws/lambda/cPaas-sNow-summarisation-agent --since 5m --format short --region us-east-1

# Test ServiceNow connection (use Python to avoid shell escaping issues)
python3 -c "
import requests
r = requests.get('https://dev380388.service-now.com/api/now/table/sys_user',
    auth=('admin', open('.env').read().split('SERVICENOW_PASSWORD=')[1].split('\n')[0]),
    headers={'Accept':'application/json'}, params={'sysparm_limit':'1'}, timeout=10)
print(r.status_code, r.headers.get('content-type'))
"

# Deploy updated code
bash deploy.sh  # (or: sed 's/^pip /pip3 /' deploy.sh | bash)

# Update Lambda env vars (use AWS Console or CLI)
aws lambda update-function-configuration \
  --function-name cPaas-sNow-summarisation-agent \
  --region us-east-1 \
  --environment file://.env.json
```

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| ServiceNow returns HTML instead of JSON | PDI is hibernating | Wake it at [developer.servicenow.com](https://developer.servicenow.com/dev.do#!/home?wu=true) |
| ServiceNow returns 401 | Wrong password (watch `I` vs `l`) | Check password in PDI portal, test with Python `requests` |
| CIRCUIT returns 401 "doesn't have access" | Wrong model name for your app key | Use `gpt-5-nano` (check EGAI portal for allowed models) |
| Bot doesn't respond at all | Webhooks not registered or wrong URL | Check with `curl` list webhooks command above |
| Welcome card shows but summary fails | Check Lambda logs for the specific error | `aws logs tail ...` command above |
| Bot responds to itself (infinite loop) | `is_bot_message()` or `_is_noise()` not catching | Add the fallback text to `BOT_FALLBACK_PHRASES` set in app.py |

---

## 13. IAM Permissions Required

The Lambda execution role needs:
- `AWSLambdaBasicExecutionRole` — CloudWatch Logs
- `lambda:InvokeFunction` on itself — for async self-invocation

The `summary_agent` IAM user (for CLI deploys) needs:
- `lambda:UpdateFunctionCode`
- `lambda:UpdateFunctionConfiguration`
- `lambda:GetFunctionConfiguration`
- `logs:TailLogEvents` (for `aws logs tail`)

---

*Last updated: 14 April 2026*
