# Twilight Agent

**Twilight Agent** is Twilight FleetZen's internal AI agent platform — a
growing set of automations that watch company WhatsApp groups, understand
messages and screenshots with an LLM, and act on them (extracting data,
writing to the FleetZen Supabase backend, replying/reacting in-chat).

The architecture is deliberately split so new capabilities can be added as
independent **tools** without touching the messaging layer:

- **Gateway** (`src/`, Node.js + Baileys) — owns the WhatsApp connection,
  filters to monitored groups, downloads media, and forwards messages to the
  agent. This layer doesn't know or care what a message means.
- **Agent** (`agent/`, Python + FastAPI) — owns understanding: an LLM picks
  the right tool for each message (vision model for screenshots, chat model
  for text/commands) and the tool does the extraction/validation/DB write.

This repo currently ships the first module — **petty cash automation**
(UPI screenshot extraction + opening-balance commands, detailed below) — but
the gateway/agent split and the tool-registry pattern (`agent/agent/tool_registry.py`)
are designed so future modules (fuel bills, expense approvals, driver
reporting, etc.) are added as new tools rather than new services.

---

## Architecture

```
WhatsApp group message
        │
        ▼
┌─────────────────────────┐      HTTP POST /process      ┌──────────────────────────┐
│  Node.js Gateway        │ ───────────────────────────▶ │  Python Agent (FastAPI)  │
│  (Baileys WebSocket)    │                              │  port 8000               │
│  src/                   │ ◀─────────────────────────── │  agent/                  │
│  • watches groups       │      JSON outcome            │  • LLM tool selection    │
│  • downloads media      │                              │  • field extraction      │
│  • sends replies/reacts │                              │  • Supabase writes       │
└─────────────────────────┘                              └──────────────────────────┘
                                                                    │
                                                          PostgREST (raw httpx)
                                                                    ▼
                                                          Supabase PostgreSQL
                                                          • vendors
                                                          • vendor_sub_categories
                                                          • petty_cash
                                                          • petty_cash_account_config
```

| Component | Tech | Start command | Auto-reload? |
|---|---|---|---|
| Gateway | Node.js + Baileys (WebSocket, no browser) | `npm run dev` (in `Agent_AI/`) | ✅ nodemon |
| Agent | Python 3.11 + FastAPI + uvicorn | `python main.py` (in `Agent_AI/agent/`) | ❌ manual restart required |

**LLM provider chain:** `NVIDIA (cloud) → OpenRouter (fallback)`.
Vision model reads screenshots directly; chat model handles text commands and
tool selection.

**Database access:** the agent talks to Supabase PostgREST directly with raw
`httpx` calls (`agent/db.py`) using the service-role key. The supabase-py SDK
is not used for these calls because its requests trigger Cloudflare 1101
"Worker threw exception" errors on this project.

---

## Message Pipeline (gateway)

1. `message` / `message_create` events both fire → deduplicated by message ID
   (5-second window in `src/index.js`).
2. **Own-reply guard** (`src/whatsapp/messageHandler.js`): every reply the bot
   sends is prefixed with an invisible zero-width space and its message ID is
   recorded. Any `fromMe` message that carries the marker or a recorded ID is
   skipped. Without this, the bot's own confirmation (which contains a month
   and an amount) would re-enter the pipeline, the LLM would call
   `set_opening_balance` again, and the bot would answer its own answers
   forever.
3. Chat-name filter: only groups listed in `WA_MONITORED_CHATS` are processed.
4. Routing by message type:
   - `chat` (text) → forwarded to agent for LLM tool selection
   - `image` / `document` → downloaded to `storage/images/received/`, queued
     (strict FIFO, one at a time), forwarded with the file path
   - anything else → skipped
5. Agent outcome → WhatsApp feedback (reactions + quoted replies, see below).

---

## Feature 1 — Petty cash extraction from UPI screenshots

**Tool:** `extract_petty_cash` (`agent/tools/petty_cash_extractor_tool.py`)

**Flow:**
1. Image goes straight to the vision LLM (no tool selection needed).
2. LLM extracts: `particular` (remarks text), `receiver_name`, `bank_name`,
   `account_holder_name` (sender), `account_last4`, `utr_number`, `date`, `amount`.
3. **Particular fallback chain:** remarks/message on the payment → receiver's
   name → *reject entry* (see Feature 4).
4. Vendor resolution from the **group name** (not the screenshot):
   - "Anudeep …" → vendor `Anudeep Cash`
   - "Venky …" → vendor `Venky Cash`
   - "Uday …" → two banks; disambiguated by the sender's masked account
     last-4 digits (`1086` → Sivapurna Kotak, `0110` → Yes Bank), falling back
     to the bank name on the receipt. If **neither** matches (e.g. an account
     ending `1111`), the entry is **rejected** — no guessing — with a quoted
     reply listing the recognised accounts and asking the sender to confirm
     the bank.
5. Validation (Feature 4) → Supabase insert (Feature 3) → audit JSON saved to
   `storage/processed/<date>/petty_cash_<message_id>_<ts>.json`.

**Test cases:**

| # | Input | Expected result |
|---|---|---|
| 1.1 | Clear UPI screenshot with remarks, UTR, amount in a monitored group | Row inserted into `petty_cash`; ✅ reaction; quoted reply "✅ Petty cash entry saved! 🧾 Ref: PC-… 💰 Amount: ₹…" |
| 1.2 | Screenshot with **no remarks** but visible receiver name | Saved with `particular` = receiver's name |
| 1.3 | Screenshot with **no remarks and no receiver** readable | NOT saved; ❌ reaction; quoted reply listing "particular" as missing |
| 1.4 | Screenshot with **no UTR** readable | NOT saved; ❌ reaction; quoted reply listing "UTR / reference number" as missing |
| 1.5 | Same screenshot sent **twice** (same UTR) | Second one NOT inserted; ⚠️ reaction; reply "already recorded (PC-…) — skipped duplicate" |
| 1.6 | Screenshot in a **non-monitored** group | Ignored entirely (logged as `[SKIP] Not monitored`) |
| 1.7 | Uday group, Paytm/PhonePe receipt masking account `xx1086` | Saved against `Uday Cash (Sivapurna Kotak)` |
| 1.8 | Uday group, receipt showing Yes Bank UPI handle | Saved against `Uday Cash (Yes Bank)` |
| 1.9 | Image download or LLM failure | ❌ reaction on the message; error logged to `logs/error.log` |
| 1.10 | Uday group, screenshot from an **unknown account** (last4 e.g. `1111`, bank name unrecognised) | NOT saved; ❌ reaction; quoted reply "could not identify the bank — account ending 1111 is not recognised" listing both Uday accounts |
| 1.11 | Anudeep/Venky group, any account last4 | Saved against that group's single vendor (last4 is not used for single-bank groups) |

---

## Feature 2 — Set opening balance (text command)

**Tool:** `set_opening_balance` (`agent/tools/set_opening_balance_tool.py`)

**Flow:**
1. Text message → LLM picks the tool. If the LLM answers in plain text instead
   (it sometimes does when fields are missing), a **deterministic fallback**
   in `agent/agent/agent.py` detects opening-balance intent (typo-tolerant
   substring check: `"bal"` + `"open"`/`"ob"`) and force-runs the tool anyway.
2. **The message text is the source of truth.** Month and amount are parsed
   from the raw message with regex (`_parse_month`, `_parse_amount`). A month
   supplied only by the LLM is *ignored* — this guards against the LLM
   hallucinating a month that was never typed (it once wrote September for a
   month-less message). Amount: text parse wins, LLM value is only a fallback.
3. Vendor lookup by group-name prefix (`Anudeep` / `Venky` / `Uday` → ILIKE on
   `vendors.display_name`). Multi-bank people (Uday) must name the bank.
4. Upsert into `petty_cash_account_config` on `(bank_id, year_month)`.

**Test cases:**

| # | Input (in monitored group) | Expected result |
|---|---|---|
| 2.1 | "set opening balance for June which is 5000" | Upsert for `2026-06`; reply "✅ Opening balance set!" with month, amount, bank |
| 2.2 | "set opening balance of 4000" (**no month**) | Nothing written; reply "❌ Month not specified. Amount detected: ₹4,000 …" |
| 2.3 | "set opening balance for June" (**no amount**) | Nothing written; reply "❌ Amount not specified. Month detected: June …" |
| 2.4 | Gibberish with intent but neither field, e.g. "set openig abalcen" | Reply "❌ Could not understand the message. Please use format: …" |
| 2.5 | Typos: "set openig abalcen of 4000 for anudeep cash" | Handled identically to 2.2 (fallback catches intent even if the LLM balks) |
| 2.6 | Uday group, no bank named, e.g. "set opening balance for June which is 5000" | Nothing written; reply "❓ Which bank account?" listing both Uday banks |
| 2.7 | Uday group with bank: "opening balance of uday cash yes bank for may as 3434" | Upsert only for `Uday Cash (Yes Bank)` |
| 2.8 | "ob june = 5k" | Amount `5000`, month `2026-06` upserted |
| 2.9 | Month with explicit year: "for May 2025 …" | Upserts `2025-05` (year taken from text) |
| 2.10 | Month without year | Current year assumed |
| 2.11 | LLM passes a hallucinated month param for a month-less message | Ignored — behaves as 2.2, nothing written |

---

## Feature 3 — Supabase persistence (petty_cash insert)

**Where:** `_save_to_db()` in `petty_cash_extractor_tool.py`, `db.insert()` in `agent/db.py`

**Conventions (match the NestJS backend and React UI exactly):**

| Column | Value |
|---|---|
| `transaction_ref` | `PC-YYYYMMDD-XXXXX` (same format as backend `generateRef()`) |
| `bank_id` | `vendors.id` looked up by `display_name` = resolved vendor name |
| `account_id` | that vendor's `sub_category_id` (→ `vendor_sub_categories.id`) — one lookup resolves both FKs |
| `amount` | **negative** (expenses reduce the running balance; same sign convention as `PettyCashEntryForm`) |
| `txn_date` | extracted date (`YYYY-MM-DD`), else today (UTC) |
| `payment_mode` | inferred from UTR format: 12 digits → `UPI`, 16 alnum → `NEFT`, 22 alnum → `RTGS`, default `UPI` |
| `particular` | remarks → receiver name (never empty — validated first) |
| `remarks` | `Added via WhatsApp from <group name>` |
| `created_by` | `NULL` (FK to ops_team; the bot has no ops user) |

**Test cases:**

| # | Scenario | Expected result |
|---|---|---|
| 3.1 | Valid entry, vendor exists | Row inserted; `_db: {saved: true, transaction_ref, id}` in result |
| 3.2 | UTR already in `petty_cash` | No insert; `_db: {saved: false, duplicate: true, existing_ref}` |
| 3.3 | Vendor name not found in `vendors` | Exception → audit JSON records the error → ❌ reaction |
| 3.4 | Amount `0` or unparseable | Exception → ❌ reaction, nothing written |
| 3.5 | Invalid/missing date | Inserted with today's date |
| 3.6 | 12-digit UTR | `payment_mode = 'UPI'` |
| 3.7 | 16-char alphanumeric UTR | `payment_mode = 'NEFT'` |
| 3.8 | Supabase/network failure mid-insert | Exception propagates; audit JSON has `_db.error`; ❌ reaction |

---

## Feature 4 — Mandatory-field validation (UTR + particular)

**Where:** `_missing_fields()` in `petty_cash_extractor_tool.py`,
`sendPettyCashFeedback()` in `src/whatsapp/messageHandler.js`

If the screenshot yields no `utr_number` or no `particular` (after the
receiver-name fallback), the entry is **not saved**. The gateway reacts ❌ and
sends a **quoted reply to the specific screenshot** so the sender knows exactly
which one failed and what to resend.

**Test cases:**

| # | Extracted fields | Saved? | WhatsApp feedback |
|---|---|---|---|
| 4.1 | particular ✓, UTR ✓ | ✅ | ✅ react + saved confirmation |
| 4.2 | particular ✗, UTR ✓ | ❌ | ❌ react + "could not read: • particular (the remarks/message on the payment)" |
| 4.3 | particular ✓, UTR ✗ | ❌ | ❌ react + "could not read: • UTR / reference number" |
| 4.4 | both ✗ | ❌ | ❌ react + both bullets listed |

The same rejection pattern applies to **unidentifiable banks** in multi-bank
groups (`_db.unknown_bank` — see test case 1.10): the entry is never booked
against a guessed bank.

The audit JSON is still written for rejected entries (with `_db.missing` or
`_db.unknown_bank`) so nothing is silently lost.

---

## Feature 5 — Self-reply loop protection

**Where:** `src/index.js` (dedup) + `src/whatsapp/messageHandler.js` (own-reply guard)

The bot account itself posts confirmations into monitored groups, and
`message_create` fires for those too. Protection layers:

1. Message-ID dedup set (same message on `message` + `message_create`).
2. Invisible zero-width-space marker prefixed to every bot reply.
3. Recorded IDs of sent replies (60-second memory).

`fromMe` messages **without** the marker still pass — so the owner can operate
the bot from its own account, including swipe-to-reply commands.

**Test cases:**

| # | Scenario | Expected |
|---|---|---|
| 5.1 | Bot sends "✅ Opening balance set! … ₹5,000" | Confirmation is NOT re-processed (no loop, no repeat upserts) |
| 5.2 | Owner types a command fresh from the bot account | Processed normally |
| 5.3 | Owner swipe-replies on someone's screenshot with a command | Processed normally (marker absent) |
| 5.4 | Any user sends a normal message | Processed normally |

---

## Feature 6 — Text-message petty cash & chit-chat handling

Text messages containing transaction details (amount, UPI ref, payee) route to
`extract_petty_cash` via LLM tool selection. Casual chit-chat matches no tool →
logged as a warning, **no reply is sent** (prevents the bot spamming groups).

| # | Input | Expected |
|---|---|---|
| 6.1 | Typed transaction details with UTR + remarks | Same save/duplicate/missing feedback as screenshots |
| 6.2 | "hello how are you" in a monitored group | No reply, warning logged |
| 6.3 | PDF document with transaction text | Text extracted via pdfplumber → LLM tool selection → same pipeline |

---

## Environment (`Agent_AI/.env`)

Key variables (see `.env.example`):

- `WA_MONITORED_CHATS` — comma-separated group-name substrings to watch
- `AGENT_SERVICE_URL` — Python agent URL (default `http://localhost:8000`)
- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` — PostgREST access (service key
  bypasses RLS — keep this file out of git; `baileys_auth/` too)
- `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, model names, timeouts (NVIDIA falls
  back to OpenRouter on error)

## Running

```bash
# Terminal 1 — Python agent (port 8000)
cd Agent_AI/agent
python main.py            # NO auto-reload: restart manually after code changes

# Terminal 2 — WhatsApp gateway (scan QR in terminal on first run)
cd Agent_AI
npm run dev               # nodemon auto-reloads on src/ changes
```

Health check: `GET http://localhost:8000/health` → `{"status":"ok","tools":[...]}`
Debug chat UI: `http://localhost:8000/` (WebSocket-fed view of processed entries)

## Logs & audit trail

| File | Contents |
|---|---|
| `logs/agent.log` | Python agent: every /process call, tool selection, saves, upserts |
| `logs/error.log` | Python agent errors only |
| `logs/gateway.log` / `gateway-error.log` | Node gateway |
| `storage/images/received/` → `processed/` | Screenshot files (moved after processing) |
| `storage/processed/<date>/petty_cash_*.json` | Audit JSON per entry, incl. `_db` save status / errors / missing fields |

## Known limitations

- Python agent has **no hot reload** — code changes need a manual restart.
- The service-role key gives the agent full DB access, including delete; a
  restricted `ai_agent` DB role (SELECT/INSERT/UPDATE only) is the planned fix.
- Uday account-last4 → bank mapping is hardcoded (`_UDAY_ACCOUNT_MAP`); replace
  with a DB lookup once an accounts table exists.
- `payment_mode` inference trusts UTR length; unusual refs default to `UPI`.
- Numeric-only month formats ("07/2026") aren't parsed — use month names or
  `YYYY-MM`.
