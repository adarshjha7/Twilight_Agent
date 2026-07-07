import re
from datetime import datetime, timezone
from tools.base_tool import BaseTool
from loguru import logger
import db

def _resolve_vendor_prefix(chat_name: str) -> str:
    """
    Extracts the person name from the WhatsApp group chat name to use
    as a prefix for ILIKE vendor lookup.

    e.g. "Anudeep Cash Test" → "Anudeep"
         "Uday Cash Test"    → "Uday"  (matches "Uday Cash Sivapurna Kotak", "Uday Cash Yes Bank", etc.)
         "Venky Cash Test"   → "Venky"
    """
    lower = chat_name.lower()
    if 'anudeep' in lower:
        return 'Anudeep'
    if 'venky' in lower:
        return 'Venky'
    if 'uday' in lower:
        return 'Uday'
    # Generic fallback: use first word of the chat name
    return chat_name.split()[0] if chat_name else chat_name


MONTH_MAP = {
    'january': '01', 'jan': '01',
    'february': '02', 'feb': '02',
    'march': '03', 'mar': '03',
    'april': '04', 'apr': '04',
    'may': '05',
    'june': '06', 'jun': '06',
    'july': '07', 'jul': '07',
    'august': '08', 'aug': '08',
    'september': '09', 'sep': '09', 'sept': '09',
    'october': '10', 'oct': '10',
    'november': '11', 'nov': '11',
    'december': '12', 'dec': '12',
}


def _parse_month(text: str) -> str | None:
    pattern = r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b'
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        month_num = MONTH_MAP[m.group(1).lower()]
        year_m = re.search(r'\b(20\d{2})\b', text)
        year = year_m.group(1) if year_m else str(datetime.now(timezone.utc).year)
        return f"{year}-{month_num}"
    ym = re.search(r'\b(20\d{2})[-/](\d{2})\b', text)
    if ym:
        return f"{ym.group(1)}-{ym.group(2)}"
    return None


def _parse_bank_hint(text: str) -> str | None:
    """Extract a bank name keyword from the message to disambiguate vendors."""
    lower = text.lower()
    checks = [
        ('yes bank', 'yes bank'), ('yesbank', 'yes bank'),
        ('kotak', 'kotak'), ('kotak mahindra', 'kotak'),
        ('hdfc', 'hdfc'),
        ('icici', 'icici'),
        ('sbi', 'sbi'), ('state bank', 'sbi'),
        ('axis', 'axis'),
        ('paytm', 'paytm'),
        ('sivapurna', 'sivapurna'),
    ]
    for keyword, canonical in checks:
        if keyword in lower:
            return canonical
    return None


def _parse_amount(text: str) -> float | None:
    # Priority: number right after 'is', '=', ':', '₹', 'rs'
    priority = re.search(r'(?:is|=|:|₹|rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(k)?', text, re.IGNORECASE)
    if priority:
        num = float(priority.group(1).replace(',', '')) * (1000 if priority.group(2) else 1)
        if num > 0:
            return num
    # Fallback: any standalone number that is not a year
    for m in re.finditer(r'\b([\d,]+(?:\.\d+)?)\s*(k)?\b', text):
        num = float(m.group(1).replace(',', '')) * (1000 if m.group(2) else 1)
        if 2020 <= num <= 2030:
            continue
        if num > 0:
            return num
    return None


class SetOpeningBalanceTool(BaseTool):
    @property
    def name(self) -> str:
        return "set_opening_balance"

    @property
    def description(self) -> str:
        return (
            "Sets the opening balance for a petty cash account for a specific month. "
            "Use when the message contains intent to set, update, or configure an opening "
            "balance for a month (e.g. 'set opening balance for June which is 5000', "
            "'opening balance june 5000', 'ob june = 5k')."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "Month in YYYY-MM format (e.g. '2026-06'). Extract from the message.",
                },
                "amount": {
                    "type": "number",
                    "description": "Opening balance amount as a plain number (e.g. 5000).",
                },
                "bank_hint": {
                    "type": "string",
                    "description": "Bank name mentioned in the message to select a specific account (e.g. 'yes bank', 'kotak', 'hdfc'). Leave empty if not mentioned.",
                },
            },
            "required": [],
        }

    async def execute(self, params: dict, context: dict) -> dict:
        text = context.get("text", "")
        chat_name = context.get("chat_name", "")

        # The raw text is the source of truth for the month: the LLM sometimes
        # hallucinates one that the message never mentions (it once passed
        # 2026-09 for a message with no month at all), which silently set the
        # balance for a random month. If _parse_month finds nothing, we ask the
        # user — never trust a month that only the LLM produced.
        month = _parse_month(text)
        # Amount: text parse wins, LLM param only as fallback (e.g. amounts
        # written in words that the regex can't read).
        amount = _parse_amount(text) or params.get("amount")

        bank_hint = (_parse_bank_hint(text) or params.get("bank_hint") or "").lower().strip()

        # Validate
        if not month and not amount:
            return {
                "reply": (
                    "❌ Could not understand the message.\n"
                    "Please use format:\n"
                    "*set opening balance for June which is 5000*"
                ),
                "success": False,
            }

        if not month:
            return {
                "reply": (
                    f"❌ *Month not specified.*\n"
                    f"Amount detected: ₹{amount:,.0f}\n\n"
                    f"Please mention the month.\n"
                    f"Example: _set opening balance for June which is {amount:,.0f}_"
                ),
                "success": False,
            }

        if not amount:
            month_display = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")
            return {
                "reply": (
                    f"❌ *Amount not specified.*\n"
                    f"Month detected: {month_display}\n\n"
                    f"Please mention the amount.\n"
                    f"Example: _set opening balance for {month_display} which is 5000_"
                ),
                "success": False,
            }

        month_display = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")

        # Extract person prefix from chat name → use to find all their bank vendors
        # e.g. "Uday Cash Test" → "Uday" → matches "Uday Cash Sivapurna Kotak", "Uday Cash Yes Bank"
        vendor_prefix = _resolve_vendor_prefix(chat_name)

        # Look up all bank vendors for this person, sorted alphabetically (matches dropdown order)
        result = await db.ilike_query("vendors", "display_name", f"{vendor_prefix}%", select="id, display_name")
        vendors = sorted(result.data or [], key=lambda v: v["display_name"])

        if not vendors:
            logger.warning(f"[SetOpeningBalance] No vendor found matching '{vendor_prefix}%'")
            return {
                "reply": f"❌ No bank account found matching *{vendor_prefix}*. Contact admin.",
                "success": False,
            }

        # If bank hint given → filter to the matching vendor
        if bank_hint:
            matched = [v for v in vendors if bank_hint in v["display_name"].lower()]
            if matched:
                vendors = matched
            else:
                names = ", ".join(v["display_name"] for v in vendors)
                return {
                    "reply": (
                        f"❌ No bank matching *{bank_hint}* found for *{vendor_prefix}*.\n"
                        f"Available banks:\n" + "\n".join(f"  • {v['display_name']}" for v in vendors)
                    ),
                    "success": False,
                }
        elif len(vendors) > 1:
            # No hint and multiple banks → ask the user to specify the full vendor name
            month_short = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B")
            bank_list = "\n".join(f"  • {v['display_name']}" for v in vendors)
            example = vendors[0]["display_name"]
            return {
                "reply": (
                    f"❓ *Which bank account?*\n"
                    f"📅 Month: {month_display}\n"
                    f"💰 Amount: ₹{amount:,.0f}\n\n"
                    f"Please specify one of:\n{bank_list}\n\n"
                    f"Example: _set opening balance of {example} to {amount:,.0f} for {month_short}_"
                ),
                "success": False,
            }
        # Single bank and no hint → proceed with it

        # Upsert opening balance for selected bank(s)
        updated = []
        for vendor in vendors:
            await db.upsert(
                "petty_cash_account_config",
                {
                    "bank_id": vendor["id"],
                    "year_month": month,
                    "opening_balance": amount,
                    "notes": f"Set via WhatsApp from {chat_name}",
                },
                on_conflict="bank_id,year_month",
            )
            updated.append(vendor["display_name"])
            logger.info(f"[SetOpeningBalance] Upserted bank_id={vendor['id']} ({vendor['display_name']}) | month={month} | amount={amount}")

        bank_lines = "\n".join(f"  • {name}" for name in updated)
        return {
            "reply": (
                f"✅ *Opening balance set!*\n\n"
                f"📅 Month: {month_display}\n"
                f"💰 Amount: ₹{amount:,.0f}\n"
                f"🏦 Bank(s):\n{bank_lines}"
            ),
            "success": True,
            "month": month,
            "amount": amount,
            "chat_name": chat_name,
            "updated_banks": updated,
        }
