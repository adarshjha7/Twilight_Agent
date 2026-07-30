import {
  ConflictException,
  Injectable,
  InternalServerErrorException,
  NotFoundException,
} from '@nestjs/common';
import { InjectDataSource } from '@nestjs/typeorm';
import { DataSource, QueryRunner } from 'typeorm';
import { CreateMaintenanceEventDto } from './dto/create-maintenance-event.dto';
import { UpdateMaintenanceEventDto } from './dto/update-maintenance-event.dto';
import { CreateIssueDto } from './dto/create-issue.dto';
import { UpdateIssueDto } from './dto/update-issue.dto';

// ═══════════════════════════════════════════════════════════════════════
// Layer 2 — MaintenanceService (single domain leaf)
//
// Owns tables: maintenance_events, maintenance_line_items,
//              maintenance_categories, maintenance_subcategories,
//              issues, issue_priorities, issue_categories
//              (issues live here because they trace into maintenance —
//               same pattern as vehicle_documents inside VehiclesService)
//
// Reads:  dataSource.query()
// Writes: queryRunner with REPEATABLE READ
//
// Pure leaf — no SupabaseService, no other Layer 2 service injected.
// Cross-domain lookups (vendors, vehicles, trip summary) use raw SQL
// JOINs or subqueries scoped to this service only.
//
// ─────────────────────────────────────────────────────────────────────
// Methods grouped by which frontend page/component consumes them:
//   Maintenance lookups:   UploadBillsPage, VehicleMaintenanceTab (dropdowns)
//   Maintenance events:    All maintenance pages + VehicleDetailPage tab
//   Maintenance writes:    UploadBillsPage, EditEventSheet, MonthWisePage edit/delete
//   Issues:                IssuePage, IssueDetailPage (via issues-page L1)
//   Cross-domain (issues): InspectionsService, InspectionFillPage, InspectionSubmissionsPage
// ═══════════════════════════════════════════════════════════════════════

export interface GetEventsFilters {
  start_date?: string;
  end_date?: string;
  vehicle_number?: string;
  payment_status?: string;
  category_id?: string;
  vendor_id?: string;
}

// Prompt used by extractBillJson() to parse a bill image/PDF into the JSON
// schema consumed by UploadBillsPage's mapJsonToForm().
const BILL_EXTRACTION_PROMPT = `You are a highly accurate invoice/bill parsing system.

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
  11. Maintenance type will always be Scheduled  or Unscheduled
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

---

Now parse the following bill attached: `;

@Injectable()
export class MaintenanceService {
  constructor(@InjectDataSource() private readonly dataSource: DataSource) {}

  // pg-node returns NUMERIC columns as strings (loss-of-precision safe).
  // Cast at the boundary so the frontend receives numbers.
  private castEventNumerics = (row: any) => {
    if (!row) return row;
    const num = (v: any) => (v == null ? v : Number(v));
    return {
      ...row,
      grand_total: num(row.grand_total),
      amount_paid: num(row.amount_paid),
      odometer_kms: num(row.odometer_kms),
      maintenance_line_items: (row.maintenance_line_items ?? []).map(
        (li: any) => ({
          ...li,
          quantity: num(li.quantity),
          unit_rate: num(li.unit_rate),
          total_amount: num(li.total_amount),
        }),
      ),
    };
  };

  private castLineItemNumerics = (row: any) => {
    if (!row) return row;
    const num = (v: any) => (v == null ? v : Number(v));
    return {
      ...row,
      quantity: num(row.quantity),
      unit_rate: num(row.unit_rate),
      total_amount: num(row.total_amount),
    };
  };

  // ═════════════════════════════════════════════════════════════════════
  // Lookups
  // Used by: UploadBillsPage, VehicleMaintenanceTab (form dropdowns)
  // ═════════════════════════════════════════════════════════════════════

  // @ used by: UploadBillsPage, VehicleMaintenanceTab, all analytics pages
  async getCategories() {
    return this.dataSource.query(
      `SELECT id, name, color, icon
       FROM maintenance_categories
       ORDER BY name`,
    );
  }

  // @ used by: UploadBillsPage, VehicleMaintenanceTab, EditEventSheet
  async getVendors() {
    return this.dataSource.query(
      `SELECT id, display_name
       FROM vendors
       ORDER BY display_name`,
    );
  }

  // @ used by: UploadBillsPage (vehicle number dropdown)
  async getVehicles() {
    return this.dataSource.query(
      `SELECT vehicle_number
       FROM vehicles
       ORDER BY vehicle_number`,
    );
  }

  // ═════════════════════════════════════════════════════════════════════
  // Events — reads
  // Used by: MaintenanceMonthWisePage, MaintenanceVehicleWisePage,
  //          MaintenanceCategoryWisePage, MaintenanceVendorWisePage,
  //          VehicleMaintenanceTab
  // ═════════════════════════════════════════════════════════════════════

  /**
   * @ used by: all maintenance analytics pages + VehicleMaintenanceTab
   *
   * Returns events joined with vendor display_name and line items aggregated
   * as JSON. Correlated subquery for line items avoids GROUP BY hash-
   * aggregate reordering (same pattern as TripsService.getAllTrips).
   */
  async getEvents(filters: GetEventsFilters) {
    const clauses: string[] = [];
    const params: any[] = [];

    if (filters.start_date) {
      params.push(filters.start_date);
      clauses.push(`me.invoice_date >= $${params.length}::date`);
    }
    if (filters.end_date) {
      params.push(filters.end_date);
      clauses.push(`me.invoice_date <= $${params.length}::date`);
    }
    if (filters.vehicle_number) {
      params.push(filters.vehicle_number);
      clauses.push(`me.vehicle_number = $${params.length}`);
    }
    if (filters.payment_status) {
      params.push(filters.payment_status);
      clauses.push(`me.payment_status = $${params.length}`);
    }
    if (filters.category_id) {
      params.push(filters.category_id);
      clauses.push(`me.category_id = $${params.length}`);
    }
    if (filters.vendor_id) {
      params.push(filters.vendor_id);
      clauses.push(`me.vendor_id = $${params.length}`);
    }

    const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';

    const rows = await this.dataSource.query(
      `SELECT
         me.event_id, me.vehicle_number, me.vendor_id, me.category_id,
         me.grand_total, me.amount_paid, me.odometer_kms,
         me.invoice_date, me.invoice_no, me.payment_status,
         me.maintenance_type, me.bill_image_url, me.metadata,
         CASE WHEN me.vendor_id IS NOT NULL
           THEN json_build_object('display_name', v.display_name)
           ELSE NULL
         END AS vendors,
         COALESCE(
           (SELECT json_agg(json_build_object(
              'line_item_id', mli.line_item_id,
              'item_category', mli.item_category,
              'part_no',       mli.part_no,
              'tyre_condition', mli.tyre_condition,
              'description',   mli.description,
              'quantity',      mli.quantity,
              'uom',           mli.uom,
              'unit_rate',     mli.unit_rate,
              'total_amount',  mli.total_amount
            ) ORDER BY mli.line_item_id)
            FROM maintenance_line_items mli
            WHERE mli.event_id = me.event_id),
           '[]'::json
         ) AS maintenance_line_items
       FROM maintenance_events me
       LEFT JOIN vendors v ON v.id = me.vendor_id
       ${where}
       ORDER BY me.invoice_date DESC
       LIMIT 500`,
      params,
    );

    return rows.map(this.castEventNumerics);
  }

  // @ used by: VehicleMaintenanceTab (lazy expand), MonthWisePage edit form
  async getLineItems(eventId: string) {
    const rows = await this.dataSource.query(
      `SELECT line_item_id, event_id, item_category, part_no, tyre_condition,
              description, quantity, uom, unit_rate, total_amount
       FROM maintenance_line_items
       WHERE event_id = $1
       ORDER BY line_item_id`,
      [eventId],
    );
    return rows.map(this.castLineItemNumerics);
  }

  /**
   * @ used by: VehicleMaintenanceTab (last-changed column in line items),
   *            MonthWisePage (part history lookup)
   *
   * For each part_no, returns the invoice_date of the most recent prior
   * event on the same vehicle before invoiceDate.
   */
  async getLastChanged(
    vehicleNumber: string,
    invoiceDate: string,
    partNos: string[],
  ) {
    const unique = [...new Set(partNos.filter(Boolean))];
    if (!unique.length) return {};

    const results: Record<string, string> = {};
    await Promise.all(
      unique.map(async (pno) => {
        try {
          const rows: { invoice_date: string }[] = await this.dataSource.query(
            `SELECT me.invoice_date
             FROM maintenance_events me
             INNER JOIN maintenance_line_items mli ON mli.event_id = me.event_id
             WHERE me.vehicle_number = $1
               AND me.invoice_date < $2::date
               AND mli.part_no = $3
             ORDER BY me.invoice_date DESC
             LIMIT 1`,
            [vehicleNumber, invoiceDate, pno],
          );
          if (rows[0]?.invoice_date) results[pno] = rows[0].invoice_date;
        } catch {
          /* non-fatal */
        }
      }),
    );
    return results;
  }

  /**
   * @ used by: useMaintenanceEnums hook (UploadBillsPage, EditEventSheet,
   *            VehicleMaintenanceTab form dropdowns)
   *
   * Reads the `maintenance_enum_values` view which exposes pg_enum
   * metadata for three maintenance-related enum types.
   */
  async getEnums(): Promise<{
    maintenanceTypes: string[];
    itemCategories: string[];
    uomOptions: string[];
  }> {
    const rows: { typname: string; enumlabel: string }[] =
      await this.dataSource.query(
        `SELECT typname, enumlabel
         FROM maintenance_enum_values
         ORDER BY enumsortorder`,
      );
    const group = (typname: string) =>
      rows.filter((r) => r.typname === typname).map((r) => r.enumlabel);
    return {
      maintenanceTypes: group('maintenance_type_enum'),
      itemCategories: group('item_category_enum'),
      uomOptions: group('uom_enum'),
    };
  }

  // @ used by: MaintenanceVehicleWisePage, MaintenanceCategoryWisePage (CPK)
  async getTripSummary(year: number, vehicleNumbers?: string[]) {
    const params: any[] = [year];
    const extra = vehicleNumbers?.length ? `AND vehicle_number = ANY($2)` : '';
    if (vehicleNumbers?.length) params.push(vehicleNumbers);

    return this.dataSource.query(
      `SELECT vehicle_number, data
       FROM vehicle_trip_yearly_summary
       WHERE year = $1 ${extra}`,
      params,
    );
  }

  // ═════════════════════════════════════════════════════════════════════
  // Writes — REPEATABLE READ transactions
  // Used by: UploadBillsPage (create), EditEventSheet / MonthWisePage (update/delete)
  // ═════════════════════════════════════════════════════════════════════

  /**
   * Insert one event + its line items + conditional odometer bump, using an
   * already-open (already-transacted) QueryRunner. Shared by createEvent
   * (single event, own transaction) and createEventsBatch (N events, one
   * shared transaction — so a failure on vehicle 3 of 4 rolls back 1 and 2
   * as well, since none of them were ever committed independently).
   */
  private async insertEventWithLineItems(
    runner: QueryRunner,
    dto: CreateMaintenanceEventDto,
  ): Promise<{ event_id: string }> {
    const { line_items, ...eventData } = dto;

    const [event]: { event_id: string }[] = await runner.query(
      `INSERT INTO maintenance_events
         (vehicle_number, vendor_id, category_id, maintenance_type,
          invoice_no, invoice_date, odometer_kms, grand_total,
          payment_status, amount_paid, bill_image_url, metadata)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       RETURNING event_id`,
      [
        eventData.vehicle_number,
        eventData.vendor_id ?? null,
        eventData.category_id ?? null,
        eventData.maintenance_type,
        eventData.invoice_no ?? null,
        eventData.invoice_date,
        eventData.odometer_kms ?? null,
        eventData.grand_total,
        eventData.payment_status,
        eventData.amount_paid ?? 0,
        eventData.bill_image_url ?? null,
        eventData.metadata ?? null,
      ],
    );

    if (line_items?.length > 0) {
      // Auto-register any part_no that isn't yet in parts_master so the
      // maintenance_line_items_part_no_fkey constraint can resolve. New part
      // numbers show up on every supplier invoice; users shouldn't have to
      // pre-register them in a catalog before recording the bill.
      const partsToUpsert = new Map<string, string>();
      for (const li of line_items) {
        if (li.part_no && li.part_no.trim()) {
          const key = li.part_no.trim();
          if (!partsToUpsert.has(key)) partsToUpsert.set(key, li.description);
        }
      }
      if (partsToUpsert.size > 0) {
        const partPlaceholders = Array.from(partsToUpsert.keys())
          .map((_, i) => `($${i * 2 + 1},$${i * 2 + 2})`)
          .join(',');
        const partParams = Array.from(partsToUpsert.entries()).flat();
        await runner.query(
          `INSERT INTO parts_master (part_no, description)
           VALUES ${partPlaceholders}
           ON CONFLICT (part_no) DO NOTHING`,
          partParams,
        );
      }

      const placeholders = line_items
        .map((_, i) => {
          const b = i * 9;
          return `($${b + 1},$${b + 2},$${b + 3},$${b + 4},$${b + 5},$${b + 6},$${b + 7},$${b + 8},$${b + 9})`;
        })
        .join(',');
      const params = line_items.flatMap((li) => [
        event.event_id,
        li.item_category,
        li.part_no ?? null,
        li.description,
        li.quantity,
        li.uom,
        li.unit_rate,
        li.total_amount,
        li.tyre_condition && li.tyre_condition.trim()
          ? li.tyre_condition.trim()
          : null,
      ]);
      await runner.query(
        `INSERT INTO maintenance_line_items
           (event_id, item_category, part_no, description, quantity, uom, unit_rate, total_amount, tyre_condition)
         VALUES ${placeholders}`,
        params,
      );
    }

    if (eventData.odometer_kms) {
      await runner.query(
        `UPDATE vehicles
         SET current_odometer = $1
         WHERE vehicle_number = $2 AND current_odometer < $1`,
        [eventData.odometer_kms, eventData.vehicle_number],
      );
    }

    return { event_id: event.event_id };
  }

  // Postgres unique_violation (23505) on maintenance_events' (vendor_id,
  // invoice_no) constraint — the only duplicate-bill check in this app.
  // Detected reactively off the INSERT failure, not a pre-submit lookup.
  // Matched by name substring, not an exact string: the constraint is
  // actually named "maintenance_events_vendor_invoice_unique" in the live
  // DB (Postgres' auto-generated default), not "uk_vendor_invoice" as the
  // schema doc describes it — match loosely so a future rename doesn't
  // silently break this again.
  private isDuplicateInvoiceError(e: any): boolean {
    const constraint = (e?.constraint ?? '').toLowerCase();
    return (
      e?.code === '23505' &&
      constraint.includes('vendor') &&
      constraint.includes('invoice')
    );
  }

  /**
   * @ used by: UploadBillsPage (POST /maintenance/events),
   *            VehicleMaintenanceTab AddEventForm
   *
   * Atomically: insert event → insert line items → bump vehicle odometer
   * (conditional — only if new reading is higher than current).
   */
  async createEvent(dto: CreateMaintenanceEventDto) {
    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      const result = await this.insertEventWithLineItems(runner, dto);
      await runner.commitTransaction();
      return result;
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      if (this.isDuplicateInvoiceError(e)) {
        throw new ConflictException('This invoice + vendor already exist.');
      }
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  /**
   * @ used by: UploadBillsPage — multi-vehicle bill split (one physical bill
   * covering several vehicles, one maintenance_event per vehicle since the
   * schema has no concept of a multi-vehicle event).
   *
   * Inserts all N events in a single transaction: if any one insert fails
   * (bad vehicle_number FK, duplicate invoice_no, etc.), every event in the
   * batch is rolled back — never a partial split.
   */
  async createEventsBatch(
    dtos: CreateMaintenanceEventDto[],
  ): Promise<{ event_ids: string[] }> {
    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      const event_ids: string[] = [];
      for (const dto of dtos) {
        const { event_id } = await this.insertEventWithLineItems(runner, dto);
        event_ids.push(event_id);
      }
      await runner.commitTransaction();
      return { event_ids };
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      if (this.isDuplicateInvoiceError(e)) {
        throw new ConflictException(
          'This invoice + vendor already exist for one or more vehicles. No records were saved.',
        );
      }
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  /**
   * @ used by: EditEventSheet (PATCH /maintenance/events/:id),
   *            MonthWisePage edit form
   *
   * Atomically: update event fields (dynamic SET) → delete + re-insert
   * all line items when line_items is provided.
   */
  async updateEvent(eventId: string, dto: UpdateMaintenanceEventDto) {
    const { line_items, ...eventData } = dto;

    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      const clean = Object.fromEntries(
        Object.entries(eventData).filter(([, v]) => v !== undefined),
      );
      if (Object.keys(clean).length > 0) {
        const cols = Object.keys(clean);
        const sets = cols.map((k, i) => `"${k}" = $${i + 2}`).join(', ');
        await runner.query(
          `UPDATE maintenance_events SET ${sets} WHERE event_id = $1`,
          [eventId, ...cols.map((k) => clean[k])],
        );
      }

      if (line_items !== undefined) {
        await runner.query(
          `DELETE FROM maintenance_line_items WHERE event_id = $1`,
          [eventId],
        );
        if (line_items.length > 0) {
          // Auto-register new part numbers (see createEvent for rationale).
          const partsToUpsert = new Map<string, string>();
          for (const li of line_items) {
            if (li.part_no && li.part_no.trim()) {
              const key = li.part_no.trim();
              if (!partsToUpsert.has(key))
                partsToUpsert.set(key, li.description);
            }
          }
          if (partsToUpsert.size > 0) {
            const partPlaceholders = Array.from(partsToUpsert.keys())
              .map((_, i) => `($${i * 2 + 1},$${i * 2 + 2})`)
              .join(',');
            const partParams = Array.from(partsToUpsert.entries()).flat();
            await runner.query(
              `INSERT INTO parts_master (part_no, description)
               VALUES ${partPlaceholders}
               ON CONFLICT (part_no) DO NOTHING`,
              partParams,
            );
          }

          const placeholders = line_items
            .map((_, i) => {
              const b = i * 9;
              return `($${b + 1},$${b + 2},$${b + 3},$${b + 4},$${b + 5},$${b + 6},$${b + 7},$${b + 8},$${b + 9})`;
            })
            .join(',');
          const params = line_items.flatMap((li) => [
            eventId,
            li.item_category,
            li.part_no ?? null,
            li.description,
            li.quantity,
            li.uom,
            li.unit_rate,
            li.total_amount,
            li.tyre_condition && li.tyre_condition.trim()
              ? li.tyre_condition.trim()
              : null,
          ]);
          await runner.query(
            `INSERT INTO maintenance_line_items
               (event_id, item_category, part_no, description, quantity, uom, unit_rate, total_amount, tyre_condition)
             VALUES ${placeholders}`,
            params,
          );
        }
      }

      await runner.commitTransaction();
      return { success: true };
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  /**
   * @ used by: MonthWisePage delete action
   *
   * FK on maintenance_line_items.event_id has ON DELETE CASCADE, so
   * deleting the parent removes children automatically. Transaction
   * kept for consistency and future-proofing.
   */
  async deleteEvent(eventId: string) {
    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      await runner.query(`DELETE FROM maintenance_events WHERE event_id = $1`, [
        eventId,
      ]);
      await runner.commitTransaction();
      return { success: true };
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  // ═════════════════════════════════════════════════════════════════════
  // Issues — owned by this L2 leaf because issues feed into maintenance
  // events. Frontend access is mediated by /issues-page (Layer 1).
  // ═════════════════════════════════════════════════════════════════════

  // Single SELECT list reused across find/list endpoints — keeps the
  // returned column shape stable for IssuePage/IssueDetailPage.
  private readonly ISSUE_COLUMNS = `
    id, issue_number, vehicle_number, category_id, summary, status,
    priority_id, due_date, source, source_submission_id, source_item_id,
    reported_by_staff_id, reported_by_staff_type, reported_at,
    assignee_id, resolved_by_id, resolved_at, closing_remarks,
    closed_by_id, closed_at, reopened_by_id, reopened_at,
    odometer_at_creation, odometer_at_resolution, work_order_id,
    seat_numbers
  `;

  // @ used by: IssuePage list + InspectionFillPage open-issues lookup
  //
  // Scopes the result based on the caller's parking_location.
  // - Parking Incharge in Hyderabad → sees only issues whose source inspection
  //   was a "Base" form (form_version_id does NOT contain 'oppbase').
  // - Parking Incharge outside Hyderabad → sees only issues whose source
  //   inspection was an "OppBase" form.
  // - Admin / Fleet Manager (parkingLocationId === null) → sees everything.
  //
  // Issues without a source_submission_id (manually created) are NOT shown to
  // PIs — only Admin/FM see them. We'd re-visit this if manual-issue volume
  // gets significant for PIs.
  async findAllIssues(parkingLocationId?: string | null) {
    if (!parkingLocationId) {
      return this.dataSource.query(
        `SELECT ${this.ISSUE_COLUMNS}
         FROM issues
         ORDER BY issue_number DESC
         LIMIT 500`,
      );
    }

    const locationRows: { city: string }[] = await this.dataSource.query(
      `SELECT city FROM parking_locations WHERE parking_id = $1`,
      [parkingLocationId],
    );
    const city = locationRows[0]?.city;
    if (!city) {
      // Fail closed when the location can't be classified.
      return [];
    }
    // Classify issues by the BUS'S CURRENT LIVE GPS LOCATION
    // (bus_current_locations.address). Whoever's at the bus right now is the
    // one who can physically resolve the issue. Vehicle numbers are
    // normalised (strip hyphens, lowercase) on both sides — same as the
    // frontend's isBusInHyderabad helper does.
    const inHyderabadExists = `
      EXISTS (
        SELECT 1 FROM bus_current_locations bcl
        WHERE REPLACE(LOWER(COALESCE(bcl.bus_number, '')), '-', '')
            = REPLACE(LOWER(issues.vehicle_number), '-', '')
          AND bcl.address ILIKE '%hyderabad%'
      )`;
    const liveLocationCondition =
      city === 'Hyderabad' ? inHyderabadExists : `NOT ${inHyderabadExists}`;

    return this.dataSource.query(
      `SELECT ${this.ISSUE_COLUMNS}
       FROM issues
       WHERE ${liveLocationCondition}
       ORDER BY issue_number DESC
       LIMIT 500`,
    );
  }

  // @ used by: IssueDetailPage (UUID-based fetch)
  async findIssueById(id: string) {
    const rows = await this.dataSource.query(
      `SELECT ${this.ISSUE_COLUMNS}
       FROM issues
       WHERE id = $1`,
      [id],
    );
    if (!rows[0]) throw new NotFoundException('Issue not found');
    return rows[0];
  }

  // @ used by: IssueDetailPage (number-based fetch from URL :issueNumber)
  async findIssueByNumber(issueNumber: number) {
    const rows = await this.dataSource.query(
      `SELECT ${this.ISSUE_COLUMNS}
       FROM issues
       WHERE issue_number = $1`,
      [issueNumber],
    );
    if (!rows[0])
      throw new NotFoundException(`Issue #${issueNumber} not found`);
    return rows[0];
  }

  // @ used by: IssuePage "Create Issue" form — reserves the next number
  async getNextIssueNumber(): Promise<{ next_number: number }> {
    const rows: { issue_number: number }[] = await this.dataSource.query(
      `SELECT issue_number
       FROM issues
       ORDER BY issue_number DESC
       LIMIT 1`,
    );
    const next_number = rows[0] ? Number(rows[0].issue_number) + 1 : 1;
    return { next_number };
  }

  // @ used by: IssuePage manual-create form + IssueDetailPage reopen
  async createIssue(dto: CreateIssueDto) {
    const cols = Object.keys(dto).filter((k) => (dto as any)[k] !== undefined);
    if (cols.length === 0) {
      throw new InternalServerErrorException(
        'Cannot create issue with empty payload',
      );
    }
    const placeholders = cols.map((_, i) => `$${i + 1}`).join(', ');
    const params = cols.map((k) => (dto as any)[k]);

    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      const rows = await runner.query(
        `INSERT INTO issues (${cols.map((c) => `"${c}"`).join(', ')})
         VALUES (${placeholders})
         RETURNING ${this.ISSUE_COLUMNS}`,
        params,
      );
      await runner.commitTransaction();
      return rows[0];
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  // @ used by: IssueDetailPage status changes, assignment, resolution
  async updateIssue(id: string, dto: UpdateIssueDto) {
    const entries = Object.entries(dto).filter(([, v]) => v !== undefined);
    if (entries.length === 0) return this.findIssueById(id);

    const setClause = entries.map(([k], i) => `"${k}" = $${i + 2}`).join(', ');
    const params = [id, ...entries.map(([, v]) => v)];

    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      const rows = await runner.query(
        `UPDATE issues SET ${setClause}
         WHERE id = $1
         RETURNING ${this.ISSUE_COLUMNS}`,
        params,
      );
      if (!rows[0]) throw new NotFoundException('Issue not found');
      await runner.commitTransaction();
      return rows[0];
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      if (e instanceof NotFoundException) throw e;
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  // @ used by: IssuePage row delete (admin tool)
  async removeIssue(id: string) {
    const runner = this.dataSource.createQueryRunner();
    await runner.connect();
    await runner.startTransaction('REPEATABLE READ');
    try {
      await runner.query(`DELETE FROM issues WHERE id = $1`, [id]);
      await runner.commitTransaction();
      return { success: true };
    } catch (e: any) {
      if (runner.isTransactionActive) await runner.rollbackTransaction();
      throw new InternalServerErrorException(e.message);
    } finally {
      await runner.release();
    }
  }

  // @ used by: IssuePage form (priority dropdown)
  async getIssuePriorities() {
    return this.dataSource.query(
      `SELECT id, name, description, color_hex, position
       FROM issue_priorities
       ORDER BY position`,
    );
  }

  // @ used by: IssuePage form (category dropdown)
  async getIssueCategories() {
    return this.dataSource.query(
      `SELECT id, name, default_priority_id, requires_odometer
       FROM issue_categories
       ORDER BY name`,
    );
  }

  // @ used by: IssueDetailPage to resolve inspection item names from
  //            stored source_item_id references.
  async getInspectionFormItems(itemIds: string[]) {
    if (!itemIds.length) return [];
    // No explicit ::uuid[] cast — matches the pattern used by InspectionsService
    // for single-id selects (`WHERE item_id = $1`). pg-node serialises the JS
    // array; Postgres performs implicit text↔uuid comparison if the column is
    // UUID typed. Explicit ::uuid[] casting was causing 500s in some envs.
    return this.dataSource.query(
      `SELECT item_id, item_name
       FROM inspection_form_items
       WHERE item_id = ANY($1)`,
      [itemIds],
    );
  }

  // @ used by: IssueDetailPage media gallery — combines media attached
  //            directly to the issue plus photos from the original
  //            inspection_item_results row (when issue originated from
  //            an inspection).
  async getIssueMedia(
    issueNumber: number,
    sourceItemId?: string,
    submissionId?: string,
  ): Promise<{ attached_media: string[]; inspection_photo_urls: string[] }> {
    const [issueRows, itemRows] = await Promise.all([
      this.dataSource.query(
        `SELECT attached_media
         FROM issues
         WHERE issue_number = $1
         LIMIT 1`,
        [issueNumber],
      ),
      sourceItemId && submissionId
        ? this.dataSource.query(
            `SELECT photo_urls
             FROM inspection_item_results
             WHERE item_id = $1 AND submission_id = $2
             LIMIT 1`,
            [sourceItemId, submissionId],
          )
        : Promise.resolve([] as { photo_urls: string[] }[]),
    ]);

    return {
      attached_media: issueRows[0]?.attached_media ?? [],
      inspection_photo_urls: itemRows[0]?.photo_urls ?? [],
    };
  }

  // ─── Dashboard helpers (already on TypeORM, moved from IssuesService) ──

  // @ used by: dashboard-page (KPI tile — open vs closed in one round-trip)
  async getDashboardIssueCounts(): Promise<{ open: number; closed: number }> {
    const [row] = await this.dataSource.query(
      `SELECT
         COUNT(*) FILTER (WHERE status IN ('Open','In_Progress','Resolved','Reopened'))::int AS open,
         COUNT(*) FILTER (WHERE status = 'Closed')::int AS closed
       FROM issues`,
    );
    return { open: row?.open ?? 0, closed: row?.closed ?? 0 };
  }

  // @ used by: dashboard-page (Issues drill-down sheet — recent open issues)
  async getDashboardOpenIssues() {
    return this.dataSource.query(
      `SELECT id, issue_number, summary, status, vehicle_number, reported_at
       FROM issues
       WHERE status IN ('Open','In_Progress','Resolved','Reopened')
       ORDER BY reported_at DESC
       LIMIT 500`,
    );
  }

  // ─── Cross-domain (called by InspectionsService) ───────────────────────

  /**
   * Insert one issue from a driver complaint inside an inspection submission.
   * Replaces the INSERT path inside `process_driver_complaints` RPC.
   *
   * `priority_id` defaults to the legacy hardcoded UUID the RPC used.
   */
  async createIssueFromInspectionComplaint(payload: {
    vehicle_number: string;
    summary: string;
    source_submission_id: string;
    source_item_id: string;
    reported_by_staff_id: string;
    reported_at: string;
    attached_media: string[] | null;
  }): Promise<{ id: string; issue_number: number }> {
    const rows = await this.dataSource.query(
      `INSERT INTO issues (
         issue_number, summary, status, vehicle_number, source,
         source_submission_id, source_item_id,
         reported_by_staff_id, reported_by_staff_type, reported_at,
         attached_media, priority_id
       )
       VALUES (
         nextval('issues_issue_number_seq'),
         $1, 'Open', $2, 'Inspection',
         $3, $4,
         $5, 'Driver', $6::timestamptz,
         $7, '7856c934-2e62-4100-a798-eb77e72d301c'::uuid
       )
       RETURNING id, issue_number`,
      [
        payload.summary,
        payload.vehicle_number,
        payload.source_submission_id,
        payload.source_item_id,
        payload.reported_by_staff_id,
        payload.reported_at,
        payload.attached_media,
      ],
    );
    return rows[0];
  }

  /**
   * Insert one issue for a failed inspection item (workflow action
   * `InspectionCreateIssueAction`). Replaces `execute_create_issue_action`
   * RPC. Differs from the complaint path in reported_by_staff_type, and
   * accepts `seat_numbers` for passenger cabin grids.
   */
  async createIssueFromFailedInspectionItem(payload: {
    vehicle_number: string;
    summary: string;
    category_id: string;
    priority_id: string;
    source_submission_id: string;
    source_item_id: string;
    reported_by_staff_id: string;
    reported_at: string;
    attached_media: string[] | null;
    seat_numbers: any[] | null;
  }): Promise<{ id: string; issue_number: number }> {
    const rows = await this.dataSource.query(
      `INSERT INTO issues (
         id, issue_number, vehicle_number, category_id, summary, status,
         priority_id, source, source_submission_id, source_item_id,
         reported_by_staff_id, reported_by_staff_type, reported_at,
         attached_media, seat_numbers
       )
       VALUES (
         gen_random_uuid(), nextval('issues_issue_number_seq'),
         $1, $2::uuid, $3, 'Open',
         $4::uuid, 'Inspection', $5, $6,
         $7, 'OpsTeam', $8::timestamptz,
         $9, $10::jsonb[]
       )
       RETURNING id, issue_number`,
      [
        payload.vehicle_number,
        payload.category_id,
        payload.summary,
        payload.priority_id,
        payload.source_submission_id,
        payload.source_item_id,
        payload.reported_by_staff_id,
        payload.reported_at,
        payload.attached_media,
        payload.seat_numbers,
      ],
    );
    return rows[0];
  }

  /**
   * Resolve a default category_id / priority_id when the workflow
   * action_config doesn't supply one.
   */
  async getDefaultCategoryAndPriorityIds(): Promise<{
    category_id: string | null;
    priority_id: string | null;
  }> {
    const [cats, pris] = await Promise.all([
      this.dataSource.query(
        `SELECT id::text AS id FROM issue_categories LIMIT 1`,
      ),
      this.dataSource.query(
        `SELECT id::text AS id FROM issue_priorities LIMIT 1`,
      ),
    ]);
    return {
      category_id: cats[0]?.id ?? null,
      priority_id: pris[0]?.id ?? null,
    };
  }

  // @ used by: InspectionSubmissionViewPage right-side panel
  async getIssuesForInspectionSubmission(
    submissionId: string,
    vehicleNumber: string,
  ) {
    return this.dataSource.query(
      `SELECT id, issue_number, summary, status, reported_at, source_item_id
       FROM issues
       WHERE vehicle_number = $1
         AND source = 'Inspection'
         AND source_submission_id = $2
       ORDER BY issue_number DESC`,
      [vehicleNumber, submissionId],
    );
  }

  // Showroom service history (read-only). Combines events + parts + part master
  // in a single round-trip so the frontend doesn't need to do 3 sequential queries.
  // @ used by: ServiceHistoryTab on VehicleDetailPage
  async getShowroomServiceHistory(vehicleNumber: string) {
    const events: any[] = await this.dataSource.query(
      `SELECT * FROM showroom_service_events
       WHERE vehicle_number = $1
       ORDER BY job_card_date DESC`,
      [vehicleNumber],
    );
    if (events.length === 0) return [];

    const eventIds = events
      .map((e) => e.service_event_id ?? e.id)
      .filter((id) => id !== undefined && id !== null);
    if (eventIds.length === 0) {
      return events.map((event) => ({
        id: event.service_event_id ?? event.id ?? event.job_card_no,
        job_card_no: event.job_card_no,
        job_card_date: event.job_card_date,
        vehicle_number: event.vehicle_number,
        kms: event.kms ?? 0,
        hrs: event.hrs ?? 0,
        dealer_name: event.dealer_name ?? '',
        dealer_city: event.dealer_city ?? '',
        parts: [],
      }));
    }

    const allParts: any[] = await this.dataSource.query(
      `SELECT * FROM showroom_service_event_parts WHERE service_event_id = ANY($1::text[])`,
      [eventIds],
    );

    const partIds = [
      ...new Set(allParts.map((p) => p.part_id).filter((id) => id)),
    ];
    const partMaster: any[] = partIds.length
      ? await this.dataSource.query(
          `SELECT * FROM showroom_parts_master WHERE part_id = ANY($1::text[])`,
          [partIds],
        )
      : [];

    const partDescriptions = new Map<string, string>();
    partMaster.forEach((pm) =>
      partDescriptions.set(pm.part_id, pm.part_description),
    );

    return events.map((event) => {
      const eventId = event.service_event_id ?? event.id;
      const eventParts = allParts
        .filter((p) => p.service_event_id === eventId)
        .map((part) => ({
          id: part.id,
          service_event_id: eventId,
          part_id: part.part_id,
          part_description:
            partDescriptions.get(part.part_id) ?? 'Unknown Part',
          quantity: part.quantity ?? 1,
          bill_to: part.bill_to ?? 'PAID',
          claim_number: part.claim_number ?? null,
        }));
      return {
        id: eventId,
        job_card_no: event.job_card_no,
        job_card_date: event.job_card_date,
        vehicle_number: event.vehicle_number,
        kms: event.kms ?? 0,
        hrs: event.hrs ?? 0,
        dealer_name: event.dealer_name ?? '',
        dealer_city: event.dealer_city ?? '',
        parts: eventParts,
      };
    });
  }

  // ── Bill JSON extraction (Gemini) ───────────────────────────────────────────
  // Used by UploadBillsPage's "Upload Bill" dialog to auto-fill the JSON field.
  async extractBillJson(file: {
    buffer: Buffer;
    originalname: string;
    mimetype: string;
  }) {
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
      throw new InternalServerErrorException(
        'GEMINI_API_KEY is not configured on the server',
      );
    }
    const model = process.env.GEMINI_MODEL || 'gemini-2.5-flash';

    const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        contents: [
          {
            parts: [
              { text: BILL_EXTRACTION_PROMPT },
              {
                inline_data: {
                  mime_type: file.mimetype || 'application/pdf',
                  data: file.buffer.toString('base64'),
                },
              },
            ],
          },
        ],
        generationConfig: { responseMimeType: 'application/json' },
      }),
    });

    if (!response.ok) {
      const errText = await response.text();
      // Gemini's free-tier rate limit — surface a short, actionable message
      // instead of the raw quota-exceeded JSON blob (nested details/links/
      // quotaDimensions) the API returns for a 429.
      if (response.status === 429) {
        throw new InternalServerErrorException(
          'Automatic parsing limit has been reached. Please try again later.',
        );
      }
      // For anything else, still avoid dumping the full raw error body —
      // pull out just Gemini's own message field when the body is JSON.
      let detail = errText;
      try {
        detail = JSON.parse(errText)?.error?.message ?? errText;
      } catch {
        // errText wasn't JSON — fall back to it as-is
      }
      throw new InternalServerErrorException(
        `Gemini API error (${response.status}): ${detail}`,
      );
    }

    const data: any = await response.json();
    const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
    if (!text) {
      throw new InternalServerErrorException(
        'Gemini returned no extractable content for this bill',
      );
    }

    const cleaned = text
      .trim()
      .replace(/^```(?:json)?/i, '')
      .replace(/```$/, '')
      .trim();
    try {
      return JSON.parse(cleaned);
    } catch {
      throw new InternalServerErrorException(
        'Gemini response was not valid JSON',
      );
    }
  }
}
