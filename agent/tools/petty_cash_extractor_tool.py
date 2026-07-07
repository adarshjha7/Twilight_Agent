import json
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

from tools.base_tool import BaseTool
from llm.llm_client import generate, generate_from_image
from config import config
import db


_EXTRACTION_FIELDS = """
FIELDS:

"particular"
  - The full text from the Remarks / Description / Message field of the transaction, copied exactly as written.
  - Example: if Remarks says "3990 tyre change", set particular to "3990 tyre change".
  - Do NOT summarise or change this text.
  - If there is NO Remarks / Description / Message field on the screenshot, set to null.

"receiver_name"
  - The name of the person or business in the To / Paid to / Sent to field — the one who RECEIVED the money.
  - This is the opposite of account_holder_name (the sender).
  - If no receiver is visible, set to null.

"bank_name"
  - The bank that processed this transaction. Look for:
    1. Text like "paid securely by <Bank>" or "powered by <Bank>"
    2. UPI handle suffix of the FROM person — map using this list:
       @kotak / @kmbl / @kotak811  → "Kotak Mahindra Bank"
       @oksbi / @sbi               → "State Bank of India"
       @okhdfcbank / @hdfc         → "HDFC Bank"
       @okicici / @icici           → "ICICI Bank"
       @okaxis / @axisbank         → "Axis Bank"
       @ybl / @yesbankltd          → "Yes Bank"
       @paytm / @pthdfc            → "Paytm Payments Bank"
       @ibl                        → "IndusInd Bank"
       @federal / @fbl             → "Federal Bank"
       @rbl                        → "RBL Bank"
       @pnb                        → "Punjab National Bank"
       @boi                        → "Bank of India"

"account_holder_name"
  - The name of the person in the FROM field — the one who SENT the money.
  - NOT the person in the To/recipient field.
  - If there is a name under paid to/ sent to do not use that as account holder name. Use the name of the person who sent the money.

"account_last4"
  - The last 4 visible digits of the FROM person's bank account number.
  - The account number is shown MASKED, and the number of mask characters varies.
    The mask can be any repetition of X, x, *, •, or dots — ALL of these are valid:
      XXXX1086 | XXXXXXXX2478 | ****0110 | ••••••2478 | xx xx 2478
  - Also match phrasings like "A/C ending 2478", "account ending in 2478", "A/c No: XXXXXXXX2478".
  - Whatever the mask length, extract ONLY the final 4 digits as a string (e.g. "1086", "2478").
  - If no account number is visible anywhere, set to null.

"utr_number"
  - The UTR format depends on the payment type. Match the label AND the format:
    • UPI / IMPS — label: "UPI Ref No.", "UPI Reference", "UPI Txn ID", "Ref No."
                   format: exactly 12 numeric digits (e.g. 618010294382)
    • NEFT        — label: "UTR No.", "UTR"
                   format: 16 alphanumeric characters, often starts with a bank code (e.g. KKBK0123456789)
    • RTGS        — label: "UTR No.", "UTR"
                   format: 22 alphanumeric characters
  - DO NOT use bank-specific internal transaction IDs (e.g. Kotak txn ID K811..., ICICI txn ID NB...) — these are NOT UTRs.
  - If no matching UTR is found, set to null.

"date"
  - Transaction date in YYYY-MM-DD format.

"amount"
  - The PRIMARY transaction amount — the large prominent number shown at the top of the receipt.
  - NEVER pick a number from the Remarks/Description/Message field as the amount.
  - Return a plain number only. No ₹, Rs., INR, or commas.
  - Examples: ₹300 → 300 | ₹1,234.50 → 1234.50
"""

IMAGE_PROMPT = f"""You are a petty cash entry extraction assistant.
Look at this UPI transaction screenshot and extract EXACTLY the following fields.
Return a single flat JSON object. No markdown, no explanation, nothing else.
{_EXTRACTION_FIELDS}
JSON:"""

TEXT_PROMPT = f"""You are a petty cash entry extraction assistant.
Extract EXACTLY the following fields from the transaction text below.
Return a single flat JSON object. No markdown, no explanation, nothing else.
{_EXTRACTION_FIELDS}
Transaction Text:
---
{{text}}
---
JSON:"""


# Hardcoded vendor resolution per group.
# For Uday groups: disambiguate by bank name first, then fall back to last 4 account digits.
# TODO: replace account_last4 hardcode with DB lookup once account table is ready.

_UDAY_KOTAK_KEYWORDS = {"kotak", "kotak mahindra", "kotak bank", "kotak811"}
_UDAY_YESBANK_KEYWORDS = {"yes bank", "yesbank", "yes"}
_UDAY_ACCOUNT_MAP = {
    "1086": "Uday Cash (Sivapurna Kotak)",
    "0110": "Uday Cash (Yes Bank)",
}


def _resolve_account_name(chat_name: str) -> str:
    """Person/sub-category level name — always derived from the group name."""
    lower = chat_name.lower()
    if "anudeep" in lower:
        return "Anudeep Cash"
    if "venky" in lower:
        return "Venky Cash"
    if "uday" in lower:
        return "Uday Cash"
    # Generic fallback: strip noise words
    stop_words = {"cash", "test", "expenses", "expense", "screenshot", "screenshots", "group", "bills", "bill", "petty"}
    words = [w for w in chat_name.split() if w.lower() not in stop_words]
    return " ".join(words).strip() or chat_name


def _resolve_vendor(chat_name: str, entry: dict) -> tuple[str, bool]:
    """
    Specific bank vendor name — hardcoded for single-bank groups, last4/bank logic for Uday.
    Returns (vendor_name, bank_detected). bank_detected is False only when an Uday
    entry defaulted to the first bank because neither last4 nor bank name matched.
    """
    name_lower = chat_name.lower()

    if "anudeep" in name_lower:
        return "Anudeep Cash", True

    if "venky" in name_lower:
        return "Venky Cash", True

    if "uday" in name_lower:
        # account_last4 is the most reliable signal (Paytm/PhonePe show the real
        # linked account masked, while bank_name may reflect the UPI gateway instead).
        last4 = str(entry.get("account_last4") or "").strip()
        if last4 in _UDAY_ACCOUNT_MAP:
            return _UDAY_ACCOUNT_MAP[last4], True
        # Fall back to bank name for direct bank UPI transfers
        bank = (entry.get("bank_name") or "").lower()
        if any(k in bank for k in _UDAY_KOTAK_KEYWORDS):
            return "Uday Cash (Sivapurna Kotak)", True
        if any(k in bank for k in _UDAY_YESBANK_KEYWORDS):
            return "Uday Cash (Yes Bank)", True
        # Neither detected — default to the first bank in the dropdown
        return "Uday Cash (Sivapurna Kotak)", False

    # Fallback for any other group name
    stop_words = {"cash", "test", "expenses", "expense", "screenshot", "screenshots", "group", "bills", "bill", "petty"}
    words = [w for w in chat_name.split() if w.lower() not in stop_words]
    return (" ".join(words).strip() or chat_name), True


# UTR format determines the payment mode (see backend PettyCashPaymentMode enum:
# Cash/UPI/NEFT/RTGS/IMPS/Cheque/Other). Screenshots are UPI receipts, so UPI is
# the default when the UTR is absent or unrecognised.
_UTR_MODE_PATTERNS = [
    (re.compile(r"^\d{12}$"), "UPI"),
    (re.compile(r"^[A-Za-z0-9]{16}$"), "NEFT"),
    (re.compile(r"^[A-Za-z0-9]{22}$"), "RTGS"),
]


def _payment_mode(utr: str | None) -> str:
    if utr:
        for pattern, mode in _UTR_MODE_PATTERNS:
            if pattern.match(utr):
                return mode
    return "UPI"


def _missing_fields(entry: dict) -> list[str]:
    """Mandatory fields that could not be extracted from the screenshot."""
    missing = []
    if not (str(entry.get("particular") or "")).strip():
        missing.append("particular (the remarks/message on the payment)")
    if not (str(entry.get("utr_number") or "")).strip():
        missing.append("UTR / reference number")
    return missing


def _generate_ref() -> str:
    # Same format the NestJS backend generates: PC-YYYYMMDD-XXXXX
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    rand = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"PC-{date}-{rand}"


class PettyCashExtractorTool(BaseTool):
    @property
    def name(self) -> str:
        return "extract_petty_cash"

    @property
    def description(self) -> str:
        return (
            "Extract structured petty cash entry data from a UPI transaction screenshot "
            "or any bill/receipt. Captures the particular (remark), bank name, "
            "account holder name, UTR number, date, transaction type (expense/credit), "
            "and amount, then saves the entry."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "priority": {
                    "type": "string",
                    "enum": ["normal", "urgent"],
                    "description": "Set to urgent if the entry needs immediate processing.",
                }
            },
            "required": [],
        }

    async def execute(self, params: dict, context: dict):
        file_path = context["file_path"]
        media_type = context["media_type"]
        message_id = context["message_id"]
        chat_name = context.get("chat_name", "")

        if media_type == "image":
            llm_response = await generate_from_image(IMAGE_PROMPT, file_path)
        else:
            llm_response = await generate(TEXT_PROMPT.format(text=context.get("text", "")))

        entry = self._parse_json(llm_response)

        # No remarks on the screenshot → use the receiver's name as the particular
        if not (entry.get("particular") or "").strip():
            entry["particular"] = entry.get("receiver_name") or None
        entry.pop("receiver_name", None)

        entry["account_name"] = _resolve_account_name(chat_name) if chat_name else None
        vendor_name, bank_detected = _resolve_vendor(chat_name, entry) if chat_name else (None, True)
        entry["vendor_name"] = vendor_name
        entry["transaction_type"] = "expense"

        # Keep the raw detection inputs before stripping internal fields — they
        # are needed for the unknown-bank rejection message below.
        detected_last4 = entry.get("account_last4")
        detected_bank = entry.get("bank_name")

        # Remove internal-use fields that should not appear in the output
        entry.pop("bank_name", None)
        entry.pop("account_holder_name", None)
        entry.pop("account_last4", None)

        if not bank_detected:
            # Vendor could not be identified — surface the missed detection as nulls
            entry["bank_name"] = None
            entry["account_last4"] = None

        entry["_meta"] = {
            "source_file": file_path,
            "media_type": media_type,
            "message_id": message_id,
            "priority": params.get("priority", "normal"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        # UTR and particular are mandatory for a petty cash entry. If the
        # screenshot didn't yield them, do NOT save — report what's missing so
        # the gateway can reply to the sender's screenshot.
        missing = _missing_fields(entry)
        if missing:
            logger.warning(f"[PettyCash] NOT saved — missing: {missing} (message {message_id})")
            entry["_db"] = {"saved": False, "missing": missing}
            self._save_audit(entry, message_id)
            return entry

        # Multi-bank group (Uday) where neither the account last4 nor the bank
        # name matched a known account: do NOT guess a bank — booking a payment
        # from an unknown account against the wrong running balance is worse
        # than rejecting. The sender must confirm which bank it came from.
        if not bank_detected:
            logger.warning(
                f"[PettyCash] NOT saved — unknown bank (last4={detected_last4!r}, "
                f"bank={detected_bank!r}) in '{chat_name}' (message {message_id})"
            )
            entry["vendor_name"] = None  # don't imply an attribution we refused to make
            entry["_db"] = {
                "saved": False,
                "unknown_bank": {
                    "last4": detected_last4,
                    "bank_options": sorted(set(_UDAY_ACCOUNT_MAP.values())),
                },
            }
            self._save_audit(entry, message_id)
            return entry

        # Persist to Supabase petty_cash. On failure the audit file still
        # records what was extracted (with the error), then the exception
        # propagates so the gateway reacts with an error.
        try:
            entry["_db"] = await self._save_to_db(entry, chat_name)
        except Exception as exc:
            entry["_db"] = {"saved": False, "error": str(exc)}
            self._save_audit(entry, message_id)
            raise

        self._save_audit(entry, message_id)
        return entry

    async def _save_to_db(self, entry: dict, chat_name: str) -> dict:
        """Insert the extracted entry into public.petty_cash. Returns a status dict."""
        try:
            amount = float(str(entry.get("amount")).replace(",", ""))
        except (TypeError, ValueError):
            raise ValueError(f"invalid amount extracted: {entry.get('amount')!r}")
        if amount == 0:
            raise ValueError("extracted amount is 0")

        vendor_name = entry.get("vendor_name")
        if not vendor_name:
            raise ValueError("no vendor resolved from the group name")

        # One lookup resolves both FKs: vendors.id → bank_id and
        # vendors.sub_category_id → account_id (vendor_sub_categories.id).
        res = await db.query("vendors", filters={"display_name": vendor_name}, select="id, sub_category_id")
        if not res.data:
            raise ValueError(f"vendor '{vendor_name}' not found in vendors table")
        bank_id = res.data[0]["id"]
        account_id = res.data[0].get("sub_category_id")
        if not account_id:
            raise ValueError(f"vendor '{vendor_name}' has no sub_category_id")

        # Idempotency (same rule as the backend): if this UTR is already in
        # petty_cash, the screenshot was processed before — do not insert twice.
        utr = (str(entry.get("utr_number") or "")).strip() or None
        if utr:
            dup = await db.query("petty_cash", filters={"utr_number": utr}, select="id, transaction_ref")
            if dup.data:
                logger.info(f"[PettyCash] Duplicate UTR {utr} — already saved as {dup.data[0]['transaction_ref']}")
                return {"saved": False, "duplicate": True, "existing_ref": dup.data[0]["transaction_ref"]}

        date_str = str(entry.get("date") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        row = {
            "transaction_ref": _generate_ref(),
            "account_id": account_id,
            "bank_id": bank_id,
            # Expenses reduce the running balance, so they are stored negative
            # (same convention as the PettyCashEntryForm UI).
            "amount": -abs(amount),
            "txn_date": f"{date_str}T00:00:00+00:00",
            "payment_mode": _payment_mode(utr),
            "utr_number": utr,
            "particular": (str(entry.get("particular") or "")).strip() or "WhatsApp entry",
            "remarks": f"Added via WhatsApp from {chat_name}" if chat_name else "Added via WhatsApp",
            "created_by": None,
        }
        result = await db.insert("petty_cash", row)
        saved = result.data[0] if result.data else row
        logger.info(f"[PettyCash] Saved {saved['transaction_ref']} | {vendor_name} | amount={row['amount']} | utr={utr}")
        return {"saved": True, "transaction_ref": saved["transaction_ref"], "id": saved.get("id")}

    def _parse_json(self, text: str) -> dict:
        cleaned = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"```", "", cleaned).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"LLM did not return JSON:\n{text}")
        return json.loads(cleaned[start : end + 1])

    # Same retention policy as processed screenshots (gateway keeps last 10 images)
    _AUDIT_KEEP_COUNT = 10

    def _save_audit(self, entry: dict, message_id: str):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        processed_root = Path(config.storage_dir) / "processed"
        audit_dir = processed_root / today
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / f"petty_cash_{message_id}_{int(datetime.now(timezone.utc).timestamp())}.json"
        audit_file.write_text(json.dumps(entry, indent=2))
        logger.info(f"Audit file saved: {audit_file}")

        # Prune: keep only the most recent N audit JSONs across all date folders
        audits = sorted(processed_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in audits[self._AUDIT_KEEP_COUNT:]:
            try:
                old.unlink()
            except OSError:
                pass  # locked/already gone — prune next time
        for day_dir in processed_root.iterdir():
            try:
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    day_dir.rmdir()
            except OSError:
                pass
