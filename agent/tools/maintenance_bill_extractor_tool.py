import json
import random
import re
import string
from datetime import datetime, timezone
from pathlib import Path
import httpx
from loguru import logger

from tools.base_tool import BaseTool
from llm import gemini_client
from config import config
import db
import supabase_storage


# Reused verbatim from the real backend's Gemini extraction prompt
# (twilight-fleetzen-backend/src/maintenance/maintenance.service.ts,
# BILL_EXTRACTION_PROMPT) so this tool's extraction behaves the same way the
# production "Upload Bill" dialog's auto-fill does — including the JSON shape
# (parsed / app_context / vehicle_groups / candidate_vehicles) and the
# multi-vehicle-bill layout reasoning. Only the trailing "Now parse the
# following bill attached:" line is replaced per media type below.
_BILL_EXTRACTION_PROMPT = """You are a highly accurate invoice/bill parsing system.

Your task is to extract structured data from a bill image or text and fill the provided JSON schema.

STRICT RULES (VERY IMPORTANT):

1. DO NOT guess or hallucinate any values.
2. If a field is not explicitly present in the bill, return:
   - "" for strings
   - null for numbers
3. Preserve exact values from the bill (no rounding unless already present).
4. Dates must be in YYYY-MM-DD format.
5. Ensure all numeric fields are numbers (not strings).
6. Tax breakdown must match totals:
   cgst + sgst + igst = tax_amount (if available)
7. grand_total = subtotal + tax_amount (if clearly available)
8. Do not modify the JSON structure.
9. Do not add extra fields.
10. If multiple line items exist, include all of them.
  11. Maintenance type will always be Scheduled or Unscheduled
FIELD-SPECIFIC RULES:

- vendor_name → Shop / Workshop / Company name
- vendor_gstin → GSTIN number
- invoice_no → Invoice / Bill number
- invoice_date → Invoice date
- odometer_kms → Vehicle reading if present
- job_card_no → Job card / Work order number
- service_advisor / technician → Names if mentioned
- workshop_branch → Branch/location name
- payment_mode → UPI / Cash / Card / etc.
- raw_notes → Any remarks or description summary

MULTIPLE VEHICLE-LIKE FIELDS ON ONE INVOICE:
- Formal tax invoices often have more than one field that can hold a
  vehicle registration number — e.g. a transport/dispatch field like
  "Motor Vehicle No." alongside a separate reference field like "Other
  References", "Reference No. & Date", "Buyer's Order No.", or "Customer
  Reference". Do not assume one of these is automatically irrelevant just
  because of its label (a "Motor Vehicle No." field can genuinely be one of
  the customer's own fleet vehicles, e.g. when goods for two of the
  customer's vehicles are billed together). Treat every vehicle-plate-shaped
  value you find, in any field, as a real candidate, and use the
  MULTI-VEHICLE BILLS and AMBIGUOUS MULTIPLE VEHICLE REFERENCES rules below
  to decide what to do when there is more than one.

LINE ITEMS:
- Extract each item separately
- item_category must be one of: PART / LABOR / LUBRICANT / OTHER
- quantity must be numeric
- unit_rate and total_amount must be numeric

MULTI-VEHICLE BILLS:
- Some bills (e.g. a handwritten workshop memo) list charges for SEVERAL
  DIFFERENT VEHICLES on one page. Two layouts are common — tell them apart
  by whether the service description repeats per vehicle or is written once:

  LAYOUT A — each vehicle has its own block of line items: a vehicle
  identifier (full or partial registration number) followed by one or more
  of its own PARTICULARS/description rows with their own amounts, then the
  next vehicle's identifier and rows, and so on.

  LAYOUT B — a single vertical numbered list of vehicle identifiers, where
  ONE service description is written only once (e.g. next to the first row,
  or as a heading over the whole list) but applies to EVERY vehicle in that
  list, and each vehicle's row has its own amount in the amount column. In
  this layout, emit one vehicle_groups entry per listed vehicle, and give
  each of them a single line item using that SAME shared description and
  THAT vehicle's own amount as both unit_rate and total_amount. The leading
  1, 2, 3, … in front of each vehicle here is that row's position in the
  list, NOT an item quantity — always use quantity 1 for these rows unless
  the bill clearly writes a separate, distinct quantity for that row.

  A bill can contain a Layout B list plus one or more separate rows
  afterward that are NOT part of that numbered list (e.g. a one-off extra
  charge with its own description and amount, not paired with a vehicle
  identifier). Emit those as their own vehicle_groups entry too, but with
  "vehicle_number" left as "" — never attach an unrelated vehicle to it.

  LAYOUT C — the vehicle identifiers are NOT in the line items at all, but
  in header/reference fields (e.g. "Motor Vehicle No." AND "Other
  References" each show a different registration number), and the invoice
  has one or more line items covering all of those vehicles together rather
  than split out per vehicle (e.g. "TYRE ... 4 Nos" when 2 vehicles were
  found — evidently 2 tyres each). When every line item's quantity divides
  EVENLY by the number of distinct vehicles found (no remainder), split it:
  emit one vehicle_groups entry per vehicle, and give each of them a copy of
  every line item with quantity = original quantity ÷ vehicle count.

  IMPORTANT — split the invoice's full final total, not the pre-tax line
  item amount: line items on formal tax invoices are usually shown BEFORE
  tax (CGST/SGST/IGST), with the tax added separately near the bottom to
  reach the "Total"/"Amount Chargeable" figure (e.g. line item ₹83,898.31 +
  GST ₹15,101.70 (net of any round-off) = Total ₹99,000.00). The split must
  fully account for that tax so nothing is lost:
    - Take the invoice's own final tax-inclusive total (the "Total" /
      "Amount Chargeable" / "grand_total" figure — NOT the pre-tax line item
      amount) and divide it evenly by the vehicle count to get each
      vehicle's total_amount for its split line item.
    - Derive that vehicle's unit_rate as its total_amount ÷ its split
      quantity, so unit_rate × quantity == total_amount for every group.
    - If there are multiple line items, first give each item its
      proportional share of the tax-inclusive total (that item's pre-tax
      amount ÷ the pre-tax subtotal of all items × the tax-inclusive total),
      then split THAT evenly across the vehicles for that item.
  If a line item's quantity does NOT divide evenly by the vehicle count (or
  there's more than one line item and the split is unclear), do not guess a
  split — treat the bill under AMBIGUOUS MULTIPLE VEHICLE REFERENCES below
  instead, so a person can allocate it manually.

- If the bill clearly covers more than one vehicle (any layout, or a mix),
  populate the top-level "vehicle_groups" array: one entry per vehicle, each
  with the vehicle identifier exactly as written on the bill (do not
  normalize, guess, or expand a partial number into a full registration
  number) and that vehicle's own line items only. In this case leave
  "app_context.vehicle_number" and "parsed.line_items" empty — the
  per-vehicle data belongs in the groups.
- If the bill covers exactly one vehicle (the normal case), leave
  "vehicle_groups" as an empty array and fill "app_context.vehicle_number" /
  "parsed.line_items" as usual — this is unchanged from before.
- Never invent or complete a vehicle number that isn't fully legible; extract
  it exactly as printed/handwritten, even if it looks incomplete.

AMBIGUOUS MULTIPLE VEHICLE REFERENCES:
- This is the fallback for when 2+ distinct vehicle-like identifiers are
  found (see MULTIPLE VEHICLE-LIKE FIELDS ON ONE INVOICE above) but LAYOUT C
  does NOT cleanly apply — i.e. there is no single evenly-divisible split of
  the line items across the vehicles found (e.g. quantities that don't
  divide evenly by the vehicle count, or multiple line items whose split
  isn't clear).
- In this situation, do NOT pick one of the vehicle numbers as THE vehicle
  and do NOT guess a split across them — you cannot reliably tell how the
  charges divide. Instead:
    - Leave "app_context.vehicle_number" empty.
    - Leave "vehicle_groups" empty — this case is not the same as the
      MULTI-VEHICLE BILLS layouts above, which only apply when the bill
      itself shows (Layout A/B) or evenly implies (Layout C) which items
      belong to which vehicle.
    - List every distinct vehicle-like identifier found, exactly as written,
      in the top-level "candidate_vehicles" array (deduplicated).
    - Fill "parsed.line_items" normally, as one flat list — a person will
      decide in the app which vehicle(s) they belong to.
- Only populate "candidate_vehicles" when there are 2+ distinct vehicle
  identifiers AND neither the explicit layouts (A/B) nor the even-split
  layout (C) applies. A bill with exactly one vehicle identifier is never
  ambiguous — use the normal single-vehicle fields for that, even if the
  same number is repeated in more than one field. Never populate more than
  one of "vehicle_groups" and "candidate_vehicles" at the same time.

NEXT SERVICE:
- Extract only if clearly mentioned

VALIDATION:
- Ensure totals match logically
- If mismatch, keep values as-is (do not fix)

OUTPUT FORMAT:
- Return ONLY valid JSON
- No explanation, no extra text

---

JSON SCHEMA:
{
  "parsed": {
    "vendor_name": "",
    "category_id": "",
    "maintenance_type": "",
    "invoice_no": "",
    "invoice_date": "",
    "odometer_kms": null,
    "grand_total": null,

    "vendor_gstin": "",
    "subtotal": null,
    "tax_amount": null,

    "tax_breakdown": {
      "cgst": null,
      "sgst": null,
      "igst": null,
      "tax_rate_percent": null
    },

    "next_service": {
      "due_kms": null,
      "due_date": ""
    },

    "job_card_no": "",
    "service_advisor": "",
    "technician": "",
    "workshop_branch": "",
    "payment_mode": "",
    "raw_notes": "",

    "line_items": [
      {
        "item_category": "",
        "description": "",
        "quantity": null,
        "uom": "",
        "unit_rate": null,
        "total_amount": null
      }
    ]
  },

  "app_context": {
    "vehicle_number": "",
    "vendor_id": null,
    "payment_status": "",
    "amount_paid": null,
    "bill_image_url": ""
  },

  "vehicle_groups": [
    {
      "vehicle_number": "",
      "odometer_kms": null,
      "line_items": [
        {
          "item_category": "",
          "description": "",
          "quantity": null,
          "uom": "",
          "unit_rate": null,
          "total_amount": null
        }
      ]
    }
  ],

  "candidate_vehicles": []
}
"""

TEXT_ONLY_PROMPT = _BILL_EXTRACTION_PROMPT + """

---

Now parse the following bill text:
---
{text}
---
"""


# Only the values the backend README documents as observed live — not guessed.
def _map_uom(hint) -> str:
    if not hint:
        return "Nos"
    lower = str(hint).strip().lower()
    if lower in ("l", "ltr", "ltrs", "liter", "liters", "litre", "litres"):
        return "Liters"
    if lower in ("kg", "kgs", "kilogram", "kilograms"):
        return "Kgs"
    if lower in ("job", "service", "lumpsum", "lump sum"):
        return "Job"
    return "Nos"


# item_category_enum only accepts PART/LABOR/LUBRICANT/SUBLET — the Gemini
# prompt (reused above) instructs PART/LABOR/LUBRICANT/OTHER, and the real
# backend's DTO/DB reject OTHER, so it's remapped to SUBLET here (see
# Maintenance README §7.4). Anything else unrecognised gets the same
# treatment rather than failing the whole bill over one bad line.
def _normalize_item_category(raw: str | None) -> str:
    up = (str(raw or "")).strip().upper()
    return up if up in ("PART", "LABOR", "LUBRICANT") else "SUBLET"


# maintenance_type_enum only accepts SCHEDULED/UNSCHEDULED. The extraction
# prompt (reused verbatim above) asks the model for this directly ("Scheduled"
# / "Unscheduled") rather than a free-text hint — trusted the same way
# production trusts Gemini for it, just re-validated against the real enum.
def _normalize_maintenance_type(raw: str | None) -> str:
    up = (str(raw or "")).strip().upper()
    return up if up in ("SCHEDULED", "UNSCHEDULED") else "UNSCHEDULED"


_CATEGORY_KEYWORDS = {
    "AC_REPAIR": ("ac ", "a/c", "air condition", "cooling"),
    "TYRE_WORK": ("tyre", "tire"),
    "BODY_WORK": ("body", "dent", "paint", "denting"),
    "SHOWROOM": ("showroom", "authorized service", "authorised service", "dealer service", "oem service"),
}


def _match_category(hint_text: str, categories: list[dict]) -> str | None:
    """Match against categories fetched live from maintenance_categories — never
    hardcode ids. Falls back to GENERAL, the app's own catch-all bucket.

    The real system's own "category_id" extraction field isn't actionable
    (Gemini has no knowledge of the live category UUIDs, and the app never
    auto-applies a guessed category FK anyway — see Maintenance README §7.8),
    so this scans the bill's own descriptive text instead."""
    by_name = {c["name"]: c["id"] for c in categories}
    lower = hint_text.lower()
    for name, keywords in _CATEGORY_KEYWORDS.items():
        if name in by_name and any(k in lower for k in keywords):
            return by_name[name]
    return by_name.get("GENERAL")


async def _resolve_vehicle(raw_number: str | None) -> tuple[dict | None, str | None]:
    """vehicle_number is a required NOT NULL FK — never invent it."""
    if not raw_number or not raw_number.strip():
        return None, "no vehicle registration number could be read from the bill"
    normalized = re.sub(r"[\s\-]", "", raw_number).upper()
    res = await db.query("vehicles", filters={"vehicle_number": normalized}, select="vehicle_number, current_odometer")
    if res.data:
        return res.data[0], None
    res2 = await db.ilike_query("vehicles", "vehicle_number", f"%{normalized}%", select="vehicle_number, current_odometer")
    if res2.data and len(res2.data) == 1:
        return res2.data[0], None
    return None, f"vehicle '{raw_number}' was not found in the fleet"


async def _resolve_vendor(vendor_name: str | None, vendor_gstin: str | None) -> tuple[str | None, str | None, list[str]]:
    """vendor_id must be resolved against a real row — the app never
    auto-applies an OCR vendor guess as a foreign key (see Maintenance README §7.8).
    Returns (vendor_id, error_reason, candidate_names_for_error)."""
    if not vendor_name and not vendor_gstin:
        return None, "no vendor name or GSTIN could be read from the bill", []

    candidates: dict[str, dict] = {}
    if vendor_name:
        for col in ("display_name", "legal_name"):
            res = await db.ilike_query("vendors", col, f"%{vendor_name}%", select="id, display_name, gst_number, is_active")
            for v in res.data or []:
                if v.get("is_active", True):
                    candidates[v["id"]] = v
    if vendor_gstin:
        res = await db.query("vendors", filters={"gst_number": vendor_gstin}, select="id, display_name, gst_number, is_active")
        for v in res.data or []:
            candidates[v["id"]] = v

    if not candidates:
        return None, f"no vendor matching '{vendor_name or vendor_gstin}' was found", []

    if len(candidates) > 1:
        if vendor_gstin:
            narrowed = [v for v in candidates.values() if v.get("gst_number") == vendor_gstin]
            if len(narrowed) == 1:
                return narrowed[0]["id"], None, []
        names = sorted(v["display_name"] for v in candidates.values())
        return None, f"multiple vendors match '{vendor_name}'", names

    only = next(iter(candidates.values()))
    return only["id"], None, []


class MaintenanceBillExtractorTool(BaseTool):
    @property
    def name(self) -> str:
        return "extract_maintenance_bill"

    @property
    def description(self) -> str:
        return (
            "Extract structured data from a vehicle maintenance/repair bill, invoice, or "
            "receipt (garage bills, service invoices, part purchases). Captures the vendor, "
            "invoice number, invoice date, vehicle number, odometer, grand total, and "
            "itemised line items, resolves them against the existing vendors/vehicles/"
            "categories, and inserts the bill into the maintenance ledger."
        )

    async def execute(self, params: dict, context: dict):
        file_path = context.get("file_path", "")
        media_type = context["media_type"]
        message_id = context["message_id"]
        chat_name = context.get("chat_name", "")
        caption = (context.get("caption") or "").strip()
        caption_suffix = (
            f"\n\nSender's caption (may include the vehicle number or other hints): {caption}"
            if caption else ""
        )

        # Images and PDFs go to Gemini as inline_data (raw bytes, base64) —
        # never pre-flattened through OCR/pdfplumber first — so Gemini sees
        # the bill exactly as the web "Upload Bill" dialog does. Plain-text
        # WhatsApp messages (no attached file) go through the text-only
        # variant of the same prompt.
        if media_type in ("image", "pdf"):
            llm_response = await gemini_client.extract_bill_json(_BILL_EXTRACTION_PROMPT + caption_suffix, file_path)
        else:
            text = context.get("text", "") or caption
            llm_response = await gemini_client.extract_bill_json(TEXT_ONLY_PROMPT.format(text=text) + caption_suffix)

        entry = self._parse_json(llm_response)
        entry["_meta"] = {
            "source_file": file_path,
            "media_type": media_type,
            "message_id": message_id,
            "chat_name": chat_name,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            entry["_db"] = await self._save_to_db(entry, chat_name, file_path)
        except Exception as exc:
            entry["_db"] = {"saved": False, "reason": "error", "detail": str(exc)}
            self._save_audit(entry, message_id)
            raise

        self._save_audit(entry, message_id)
        return entry

    async def _save_to_db(self, entry: dict, chat_name: str, file_path: str) -> dict:
        parsed = entry.get("parsed") or {}
        app_context = entry.get("app_context") or {}
        vehicle_groups = entry.get("vehicle_groups") or []
        candidate_vehicles = entry.get("candidate_vehicles") or []

        # Vendor, category, maintenance_type and metadata are bill-level — one
        # vendor/workshop issues the whole invoice regardless of how many
        # vehicles it covers, so these are resolved once and reused for every
        # vehicle+bill event below.
        # vendor_id is nullable on maintenance_events — an unresolved vendor no
        # longer blocks the whole bill from being recorded. It's saved with
        # vendor_id=null and a vendor_note so it can be found and reconciled
        # later (e.g. once the vendor is registered, or via the app's edit UI),
        # rather than the bill being lost entirely.
        vendor_id, verr2, candidates = await _resolve_vendor(parsed.get("vendor_name"), parsed.get("vendor_gstin"))
        vendor_note = None
        if verr2:
            logger.warning(f"[Maintenance] Vendor not resolved ({verr2}) — saving with vendor_id=null")
            vendor_note = verr2
            vendor_id = None

        date_str = str(parsed.get("invoice_date") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        invoice_no = (str(parsed.get("invoice_no") or "")).strip() or None

        top_line_items = parsed.get("line_items") or []
        category_text = " ".join(filter(None, [
            parsed.get("raw_notes"),
            parsed.get("workshop_branch"),
            parsed.get("vendor_name"),
            *[li.get("description", "") for li in top_line_items],
            *[li.get("description", "") for g in vehicle_groups for li in (g.get("line_items") or [])],
        ]))
        categories = await db.query("maintenance_categories", select="id, name")
        category_id = _match_category(category_text, categories.data or [])
        maintenance_type = _normalize_maintenance_type(parsed.get("maintenance_type"))
        metadata = self._build_metadata(parsed, chat_name)

        # Each vehicle+bill combination is an independent maintenance event.
        #   - vehicle_groups: the bill already gives (or the model already
        #     split) a per-vehicle amount — use it directly.
        #   - candidate_vehicles: the model couldn't tell how charges split,
        #     so the overall total is divided equally across them.
        #   - neither: the normal single-vehicle bill.
        if vehicle_groups:
            entries = [
                (g.get("vehicle_number"), g.get("line_items") or [], g.get("odometer_kms"))
                for g in vehicle_groups
            ]
        elif candidate_vehicles:
            entries = [(vn, [], None) for vn in candidate_vehicles]
        else:
            entries = [(app_context.get("vehicle_number"), top_line_items, parsed.get("odometer_kms"))]

        # Filter out a Layout-B "unrelated extra row" entry (vehicle_number
        # left "" by design per the extraction prompt) — it can't be
        # attributed to any vehicle, so it can't become a maintenance_event.
        attributable = [e for e in entries if e[0]]
        unattributed_count = len(entries) - len(attributable)
        multi = len(attributable) > 1

        overall_total = None
        try:
            overall_total = float(str(parsed.get("grand_total")).replace(",", ""))
        except (TypeError, ValueError):
            pass
        equal_share = round(overall_total / len(attributable), 2) if overall_total and attributable else None

        # ── Phase 1: validate every vehicle in the batch, no writes yet ──────
        # Mirrors createEventsBatch()'s shared-transaction guarantee (any one
        # vehicle failing means NONE of the batch is saved) to the extent this
        # agent's DB access allows: it can INSERT/UPDATE but not run a real
        # multi-table transaction or DELETE to roll back (see Known
        # limitations). Resolving/validating everything up front — before any
        # row exists — means the ordinary failure modes (bad vehicle number,
        # already-recorded invoice, no usable amount) abort the whole batch
        # cleanly, with nothing written. invoice_no is the bill's own value,
        # unmodified and identical across every vehicle in the batch — the
        # backend's (vendor_id, invoice_no) duplicate guard is existing
        # production behaviour, not something this tool works around.
        plans = []
        for vehicle_raw, raw_items, odometer_raw in attributable:
            plan, err = await self._validate_vehicle_entry(
                vehicle_raw, raw_items, equal_share, odometer_raw, vendor_id, invoice_no,
            )
            if err:
                logger.warning(f"[Maintenance] Batch NOT saved — vehicle '{vehicle_raw}' failed validation: {err}")
                return {
                    "saved": False,
                    "multi_vehicle": multi,
                    "reason": err["reason"],
                    "detail": err.get("detail"),
                    "vehicle_raw": vehicle_raw,
                    "vehicle_number": err.get("vehicle_number"),
                    "existing_event_id": err.get("existing_event_id"),
                }
            plans.append(plan)

        if not plans:
            return {"saved": False, "reason": "vehicle_not_found", "detail": "no vehicle could be identified on this bill"}

        # ── Phase 2: every vehicle passed validation — write them all ───────
        # A multi-vehicle bill's one receipt photo is shared by every event in
        # the batch (same convention as the real app's `multi-vehicle/<ts>-<rand>.<ext>`
        # storage path — see README §3.2) — uploaded once, not per vehicle.
        shared_bill_image_url = None
        if multi and file_path:
            shared_bill_image_url = await self._upload_bill_image(file_path, self._multi_vehicle_image_path(file_path))

        results = []
        for plan in plans:
            results.append(await self._insert_vehicle_event(
                plan=plan,
                vendor_id=vendor_id,
                category_id=category_id,
                maintenance_type=maintenance_type,
                invoice_no=invoice_no,
                date_str=date_str,
                metadata=metadata,
                file_path=None if multi else file_path,
                precomputed_bill_image_url=shared_bill_image_url,
                vendor_note=vendor_note,
            ))

        if not multi:
            result = results[0]
            if unattributed_count:
                result["unattributed_rows"] = unattributed_count
            return result

        return {
            "saved": all(r.get("saved") for r in results),
            "multi_vehicle": True,
            "vehicle_count": len(results),
            "events": results,
            "unattributed_rows": unattributed_count or None,
        }

    async def _validate_vehicle_entry(
        self, vehicle_raw, raw_items, fallback_total, odometer_raw, vendor_id, invoice_no,
    ) -> tuple[dict | None, dict | None]:
        """Read-only checks for one vehicle+bill combination — resolves the
        vehicle, checks for a prior duplicate, and works out this event's line
        items/total, without writing anything. Returns (plan, None) on success
        or (None, error) on failure."""
        vehicle, verr = await _resolve_vehicle(vehicle_raw)
        if verr:
            return None, {"reason": "vehicle_not_found", "detail": verr}

        # Scoped to this vehicle as well as (vendor_id, invoice_no): several
        # vehicles legitimately share one invoice_no on a multi-vehicle bill
        # (see createEventsBatch), so a bare (vendor_id, invoice_no) check
        # would wrongly flag the 2nd+ vehicle as a duplicate of the 1st. This
        # narrows to "has THIS vehicle already been recorded against this
        # invoice" — the DB's own (vendor_id, invoice_no) constraint still
        # applies underneath and is caught below if it fires for real.
        # vendor_id may be None (unresolved vendor, saved as null) — omit it
        # from the filter rather than matching literally on "null" as a string.
        if invoice_no:
            dup_filters = {"invoice_no": invoice_no, "vehicle_number": vehicle["vehicle_number"]}
            if vendor_id:
                dup_filters["vendor_id"] = vendor_id
            dup = await db.query("maintenance_events", filters=dup_filters, select="event_id")
            if dup.data:
                return None, {
                    "reason": "duplicate",
                    "existing_event_id": dup.data[0]["event_id"],
                    "vehicle_number": vehicle["vehicle_number"],
                }

        prepared_items, grand_total = self._prepare_line_items(raw_items, fallback_total, None)
        if not prepared_items:
            return None, {
                "reason": "amount_unresolved",
                "detail": "no line items or usable total for this vehicle",
                "vehicle_number": vehicle["vehicle_number"],
            }

        odometer = odometer_raw
        try:
            odometer = int(odometer) if odometer is not None else None
        except (TypeError, ValueError):
            odometer = None

        return {
            "vehicle": vehicle,
            "prepared_items": prepared_items,
            "grand_total": grand_total,
            "odometer": odometer,
        }, None

    async def _insert_vehicle_event(
        self, *, plan, vendor_id, category_id, maintenance_type, invoice_no, date_str,
        metadata, file_path, precomputed_bill_image_url, vendor_note=None,
    ) -> dict:
        vehicle = plan["vehicle"]
        prepared_items = plan["prepared_items"]
        grand_total = plan["grand_total"]
        odometer = plan["odometer"]

        event_row = {
            "vehicle_number": vehicle["vehicle_number"],
            "vendor_id": vendor_id,
            "category_id": category_id,
            "maintenance_type": maintenance_type,
            "invoice_no": invoice_no,
            "invoice_date": date_str,
            "odometer_kms": odometer,
            "grand_total": grand_total,
            "metadata": metadata,
        }

        try:
            result = await db.insert("maintenance_events", event_row)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409 or "vendor_invoice_unique" in exc.response.text:
                return {"saved": False, "reason": "duplicate", "vehicle_number": vehicle["vehicle_number"]}
            raise
        event = result.data[0]
        event_id = event["event_id"]

        # Parts auto-registration — ON CONFLICT DO NOTHING, same as the backend.
        part_rows = [
            {"part_no": i["part_no"], "description": i["description"]}
            for i in prepared_items if i["part_no"]
        ]
        if part_rows:
            try:
                await db.insert_ignore("parts_master", part_rows, on_conflict="part_no")
            except Exception as exc:
                logger.warning(f"[Maintenance] parts_master registration failed (non-fatal): {exc}")

        for item in prepared_items:
            item["event_id"] = event_id
        try:
            await db.insert("maintenance_line_items", prepared_items)
        except Exception as exc:
            # The agent's DB access deliberately has no DELETE — it cannot roll
            # back the event it already created. This is a known, documented
            # limitation of writing directly to Supabase rather than through the
            # backend's transactional insert.
            logger.error(
                f"[Maintenance] Line items insert FAILED after event {event_id} was created — "
                f"event is orphaned with no line items. Manual cleanup needed. Error: {exc}"
            )
            return {
                "saved": False,
                "reason": "line_items_failed",
                "orphaned_event_id": event_id,
                "detail": str(exc),
                "vehicle_number": vehicle["vehicle_number"],
            }

        if odometer:
            try:
                await db.update(
                    "vehicles",
                    filters={"vehicle_number": f"eq.{vehicle['vehicle_number']}", "current_odometer": f"lt.{odometer}"},
                    data={"current_odometer": odometer},
                )
            except Exception as exc:
                logger.warning(f"[Maintenance] Odometer bump failed (non-fatal): {exc}")

        bill_image_url = precomputed_bill_image_url
        if not bill_image_url and file_path:
            ext = Path(file_path).suffix or ".jpg"
            bill_image_url = await self._upload_bill_image(file_path, f"{vehicle['vehicle_number']}/{event_id}{ext}")
        if bill_image_url:
            try:
                await db.update(
                    "maintenance_events",
                    filters={"event_id": f"eq.{event_id}"},
                    data={"bill_image_url": bill_image_url},
                )
            except Exception as exc:
                logger.warning(f"[Maintenance] Bill image URL patch failed (non-fatal): {exc}")

        logger.info(
            f"[Maintenance] Saved event_id={event_id} vehicle={vehicle['vehicle_number']} "
            f"vendor_id={vendor_id} total={grand_total} items={len(prepared_items)}"
        )
        result = {
            "saved": True,
            "event_id": event_id,
            "vehicle_number": vehicle["vehicle_number"],
            "grand_total": grand_total,
            "bill_image_url": bill_image_url,
        }
        if vendor_note:
            result["vendor_note"] = vendor_note
        return result

    async def _upload_bill_image(self, file_path: str, remote_path: str) -> str | None:
        try:
            return await supabase_storage.upload_file(file_path, "maintenance_bills", remote_path)
        except Exception as exc:
            logger.warning(f"[Maintenance] Bill image upload failed (non-fatal): {exc}")
            return None

    def _multi_vehicle_image_path(self, file_path: str) -> str:
        ext = Path(file_path).suffix or ".jpg"
        rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"multi-vehicle/{ts}-{rand}{ext}"

    def _build_metadata(self, parsed: dict, chat_name: str) -> dict:
        metadata = {
            "source": "whatsapp",
            "chat_name": chat_name,
            "vendor_gstin": parsed.get("vendor_gstin") or None,
            "subtotal": parsed.get("subtotal"),
            "tax_amount": parsed.get("tax_amount"),
            "tax_breakdown": parsed.get("tax_breakdown") or None,
            "next_service": parsed.get("next_service") or None,
            "job_card_no": parsed.get("job_card_no") or None,
            "service_advisor": parsed.get("service_advisor") or None,
            "technician": parsed.get("technician") or None,
            "workshop_branch": parsed.get("workshop_branch") or None,
            "payment_mode": parsed.get("payment_mode") or None,
            "raw_notes": parsed.get("raw_notes") or None,
        }
        return {k: v for k, v in metadata.items() if v not in (None, "", {})}

    def _prepare_line_items(self, raw_items: list, fallback_total: float | None, vendor_name: str | None) -> tuple[list[dict], float]:
        """Returns (prepared_line_items, event_grand_total). Empty list means
        this vehicle+bill combination has no usable amount at all (no line
        items and no fallback total to split) — the caller must reject it
        rather than insert a bill with an invented total."""
        prepared = []
        for li in raw_items:
            try:
                qty = float(li.get("quantity") or 1)
                unit_rate = float(li.get("unit_rate"))
                total_amt = (
                    float(li.get("total_amount"))
                    if li.get("total_amount") is not None
                    else round(qty * unit_rate, 2)
                )
            except (TypeError, ValueError):
                continue  # skip one unparsable row rather than fail the whole bill
            part_no = li.get("part_no")
            prepared.append({
                "item_category": _normalize_item_category(li.get("item_category")),
                "part_no": (str(part_no).strip() or None) if part_no else None,
                "description": (str(li.get("description") or "")).strip() or "Item",
                "quantity": qty,
                "uom": _map_uom(li.get("uom")),
                "unit_rate": unit_rate,
                "total_amount": total_amt,
            })

        if not prepared:
            if not fallback_total or fallback_total <= 0:
                return [], 0.0
            # No itemised breakdown for this vehicle — record it as a single
            # lump-sum line (the bill's own total, or this vehicle's equal
            # share of it) so it's still captured.
            prepared = [{
                "item_category": "SUBLET",
                "part_no": None,
                "description": f"Bill from {vendor_name}" if vendor_name else "Maintenance bill",
                "quantity": 1,
                "uom": "Job",
                "unit_rate": fallback_total,
                "total_amount": fallback_total,
            }]
            return prepared, fallback_total

        items_sum = round(sum(i["total_amount"] for i in prepared), 2)
        if fallback_total and abs(items_sum - fallback_total) > max(1.0, fallback_total * 0.02):
            logger.warning(f"[Maintenance] Line items sum {items_sum} does not match bill total {fallback_total}")
        return prepared, items_sum

    def _parse_json(self, text: str) -> dict:
        cleaned = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"```", "", cleaned).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"LLM did not return JSON:\n{text}")
        return json.loads(cleaned[start : end + 1])

    # Own audit subdirectory (not storage/processed/) so this tool's pruning
    # never evicts petty cash's audit files, and vice versa.
    _AUDIT_KEEP_COUNT = 10

    def _save_audit(self, entry: dict, message_id: str):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        processed_root = Path(config.storage_dir) / "processed_maintenance"
        audit_dir = processed_root / today
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / f"maintenance_{message_id}_{int(datetime.now(timezone.utc).timestamp())}.json"
        audit_file.write_text(json.dumps(entry, indent=2))
        logger.info(f"Audit file saved: {audit_file}")

        audits = sorted(processed_root.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in audits[self._AUDIT_KEEP_COUNT:]:
            try:
                old.unlink()
            except OSError:
                pass
        for day_dir in processed_root.iterdir():
            try:
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    day_dir.rmdir()
            except OSError:
                pass
