# Maintenance Module — Technical README

> Written for: anyone integrating with the Maintenance module programmatically — specifically, an AI agent that
> pulls maintenance bills from WhatsApp, parses them, and inserts them into FleetZen via the **existing** APIs
> and business logic, without modifying that logic.
>
> Scope: end-to-end bill lifecycle (upload → storage → parsing → validation → DB insertion → downstream usage),
> database schema, backend APIs, frontend pages, business rules, and integration guidance.
>
> Researched directly from the live codebase on 2026-07-24. Where the committed repo is missing or stale
> (schema files, PRD docs), that is called out explicitly rather than papered over.

---

## Table of Contents

1. [Overall Workflow](#1-overall-workflow)
2. [Database Schema](#2-database-schema)
3. [Bill Lifecycle](#3-bill-lifecycle-upload--storage--parsing--validation--insertion--approval--downstream)
4. [Backend APIs & Services](#4-backend-apis--services)
5. [Frontend Pages & Components](#5-frontend-pages--components)
6. [Key Functions by Step](#6-key-functions-by-step)
7. [Business Rules & Validations](#7-business-rules--validations)
10. [Assumptions, Edge Cases, Implementation Notes](#10-assumptions-edge-cases-implementation-notes)

---

## 1. Overall Workflow

At a high level, a maintenance bill goes through:

```
User opens "Upload Bill"  →  picks/photographs a bill file  →  (optional but compulsory in this case) Gemini OCR extraction
   →  form is auto-filled / manually filled  →  client-side validation
   →  event + line items inserted (one DB transaction)  →  receipt image uploaded to Supabase Storage
   →  event patched with the image URL  →  bill is immediately "live" (no approval gate)
   →  shows up in Month-Wise ledger, and in Vehicle-/Category-/Vendor-Wise analytics
```

There is **no formal approval/verification workflow** for maintenance bills (see [§3.5](#35-approvalverification) and
[§7](#7-business-rules--validations)). The only state tracked per bill is a payment-status flag
(`UNPAID` / `PARTIAL` / `PAID`), which is informational, not a gate.

Two independent domains share one backend service (`MaintenanceService`) because "issues trace into maintenance":
**maintenance bills** (`maintenance_events` + `maintenance_line_items`) and **issues** (`issues` table, driver-reported
problems). This README focuses on bills; issues are mentioned only where they intersect.

---

## 2. Database Schema

### 2.0 Important caveat — the schema is not in this repo's committed migrations

`supabase/migrations/` (5 files) and the generated `src/integrations/supabase/types.ts` contain **no** maintenance
tables at all. The live schema was applied to Supabase out-of-band and was never captured as a migration or
regenerated into `types.ts`. The closest artifacts in the repo are:

- `maintenance_prd.sql` (repo root) — an original design script, **partially stale** (see drift notes below).
- `src/integrations/supabase/Maintenance_prd.md` — narrative PRD (KPI definitions, page specs).
- `twilight-fleetzen-backend/src/maintenance/maintenance.service.ts` — raw SQL against the *live* schema. **Treat
  this file as ground truth**, not the PRD script, when the two disagree.

If you need to introspect the real, current schema, do it directly against the live Postgres instance
(`\d+ maintenance_events`, `\d+ maintenance_line_items`, `SELECT * FROM maintenance_enum_values`) rather than
trusting the committed SQL files.

### 2.1 `maintenance_categories`

```sql
id text PRIMARY KEY, name text NOT NULL, color text DEFAULT '#6B7280', icon text DEFAULT 'Wrench', created_at timestamptz
```
Seeded values: `SHOWROOM`, `BODY_WORK`, `AC_REPAIR`, `TYRE_WORK`, `GENERAL`.

### 2.2 `vendors` (shared with Payments module — not maintenance-only)

```sql
id uuid PK, legal_name, display_name NOT NULL, contact_person, phone_number,
bank_name, bank_account_number, ifsc_code, gst_number, is_active bool DEFAULT true,
category_id uuid, sub_category_id uuid, description, default_vendor_allocation_type_id uuid,
entities_selected jsonb, created_at, updated_at
```
Maintenance only ever reads `id, display_name` from this table. It is the exact same table used by
Petty Cash / vendor payments (see `docs/PAYMENTS_DEVELOPER_GUIDE.md`) — creating a vendor from either module
creates the same row.

### 2.3 `maintenance_events` (bill header)

| Column | Type | Notes |
|---|---|---|
| `event_id` | `bigint identity` PK | |
| `vehicle_number` | `text` NOT NULL | FK → `vehicles.vehicle_number` |
| `vendor_id` | `uuid` nullable | FK → `vendors.id` |
| `category_id` | `text` nullable | FK → `maintenance_categories.id` |
| `maintenance_type` | `text` NOT NULL | `'SCHEDULED' \| 'UNSCHEDULED'` |
| `invoice_no` | `text` nullable | |
| `invoice_date` | `date` NOT NULL | |
| `odometer_kms` | `integer` nullable | |
| `grand_total` | `numeric(12,2)` NOT NULL | |
| `payment_status` | `text` DEFAULT `'UNPAID'` | `'UNPAID' \| 'PARTIAL' \| 'PAID'` |
| `amount_paid` | `numeric(12,2)` DEFAULT 0 | |
| `bill_image_url` | `text` nullable | **not in the PRD script — added later**; Supabase Storage public URL |
| `metadata` | `jsonb` DEFAULT `{}` | tax breakdown, next-service, job card, etc. |
| `created_at` | `timestamptz` DEFAULT now() | |

**Unique constraint** on `(vendor_id, invoice_no)` — the app's only duplicate-bill guard (see §7). The PRD script
names it `uk_vendor_invoice`, but the backend explicitly documents that the *live* DB name is Postgres's
auto-generated default (`maintenance_events_vendor_invoice_unique`), and matches on it by substring rather than
exact name (`maintenance.service.ts:638-653`).

### 2.4 `maintenance_line_items` (bill detail)

| Column | Type | Notes |
|---|---|---|
| `line_item_id` | `bigint identity` PK | |
| `event_id` | `bigint` NOT NULL | FK → `maintenance_events.event_id`, **ON DELETE CASCADE** |
| `item_category` | `text` NOT NULL | `'PART' \| 'LABOR' \| 'LUBRICANT' \| 'SUBLET'` |
| `part_no` | `text` nullable | FK-like → `parts_master.part_no` (`maintenance_line_items_part_no_fkey`) |
| `tyre_condition` | `text` nullable | `'New Tyre' \| 'Second Hand' \| 'Retreaded' \| null` — **not in the PRD**, drives tyre classification (§10) |
| `description` | `text` NOT NULL | |
| `quantity` | `numeric(10,3)` DEFAULT 1 | |
| `uom` | `text` DEFAULT `'Nos'` | dynamic — see enums below |
| `unit_rate` | `numeric(12,2)` NOT NULL | |
| `total_amount` | `numeric(12,2)` NOT NULL | |

Note the live schema uses a **text `part_no`**, not the PRD's `part_id bigint FK` design — see §2.5.

### 2.5 `parts_master`

```sql
part_id bigint identity PK, part_no text UNIQUE NOT NULL, description text NOT NULL, created_at timestamptz
```
Any `part_no` not yet present is **auto-inserted** (`ON CONFLICT (part_no) DO NOTHING`) as part of the same
transaction that creates a bill — there is no separate "register a part first" step (§7).


### 2.8 Enums

No maintenance enum types exist in `types.ts`. The live enums are real Postgres `enum` types, exposed via a
view (`maintenance_enum_values`, DDL not present in this repo) that `MaintenanceService.getEnums()`
(`maintenance.service.ts:492-517`) reads:

- **`maintenance_type_enum`**: `SCHEDULED`, `UNSCHEDULED`
- **`item_category_enum`**: `PART`, `LABOR`, `LUBRICANT`, `SUBLET`
- **`uom_enum`**: dynamic, not hardcoded anywhere — fetch via `GET /maintenance-month-wise-page/enums`. Observed
  values include `Nos`, `Liters`, `Kgs`, `Job`.
- **`payment_status`**: `UNPAID`, `PARTIAL`, `PAID` — a `text` + CHECK, not a `pg_enum` type.
- **`tyre_condition`**: `New Tyre`, `Second Hand`, `Retreaded` — app-level validation only (DTO `@IsIn`), not one
  of the three enums the view exposes.
- **No approval/verification enum exists** anywhere in the schema.

### 2.9 Relationships summary

```
vehicles (vehicle_number) ──< maintenance_events >── vendors (id)
                                     │
                                     ├──< maintenance_line_items >── parts_master (part_no)
                                     │
                              maintenance_categories (id)

maintenance_events.bill_image_url  →  Supabase Storage bucket "maintenance_bills" (plain text URL, no FK)
```

There is **no `uploaded_by`/`approved_by` column** on `maintenance_events`. Attribution lives only in the
app-level audit log, `user_activity` (`supabase/migrations/20260708000000_create_user_activity.sql`), written
by the controller after every create (`upload-bills-page.controller.ts:69-83`).

### 2.10 RLS note

`maintenance_prd.sql` shows permissive RLS policies (`FOR ALL TO authenticated USING (true) WITH CHECK (true)`
on `maintenance_events`/`maintenance_line_items`). **The app does not rely on RLS for authorization** — the
NestJS backend enforces its own RBAC (`CookieAuthGuard` + `PermissionsGuard`, §4.3). If RLS is indeed still this
open on the live project, writing directly to Postgres (bypassing the API) would skip the app's entire
authorization layer — another reason to integrate via the REST API, not direct DB writes (§9).

---

## 3. Bill Lifecycle: Upload → Storage → Parsing → Validation → Insertion → Approval → Downstream

### 3.1 Upload

Entry point: `UploadBillsPage.tsx` (`src/pages/UploadBillsPage.tsx`, route `/maintenance/upload-bills`).
"Upload Bill" button opens a dialog with a file `<input accept=".png,.jpg,.jpeg,.pdf">`
(`UploadBillsPage.tsx:1746-1752`). **No file-size validation exists anywhere**, client or server; the `accept`
attribute is advisory only.

### 3.2 Storage

Two-step **signed-URL** pattern — the backend never receives the raw bill bytes for permanent storage:

1. Frontend calls `POST /upload-bills-page/signed-upload-url` with `{ bucket: 'maintenance_bills', path }`.
2. Backend (`UploadBillsPageService.createSignedUploadUrl`, `upload-bills-page.service.ts:49-62`) calls
   `supabase.storage.from(bucket).createSignedUploadUrl(path)` and returns `{ signedUrl, token, publicUrl }`.
3. Frontend `PUT`s the (client-compressed — see §3.3) file bytes directly to `signedUrl`.
4. Frontend calls `PATCH /upload-bills-page/events/:eventId { bill_image_url: publicUrl }` to attach the URL to
   the already-created event row.

Path convention: `${vehicle_number}/${eventId}.${ext}` for a single-vehicle bill;
`multi-vehicle/${Date.now()}-${rand}.${ext}` for a multi-vehicle batch (all vehicles in that batch share **one**
receipt image/URL).

### 3.3 Parsing

Optional, decoupled step — `POST /upload-bills-page/extract-bill` (multipart `file`) →
`MaintenanceService.extractBillJson()` (`maintenance.service.ts:1324-1402`) → Google Gemini
(`gemini-2.5-flash` by default; `GEMINI_API_KEY`/`GEMINI_MODEL` env vars) with a long, precise extraction prompt
(`BILL_EXTRACTION_PROMPT`, `maintenance.service.ts:50-288`). Returns JSON only — **this call does not write
anything to the database.** See §6 for the JSON shape and §9 for how the WhatsApp agent should reuse this.

Client-side, the returned JSON is mapped into form state by `mapJsonToForm()`
(`src/pages/UploadBillsPage.tsx:261-376`), which also detects genuinely multi-vehicle bills.

A separate client-side step, `compressImage()` (`UploadBillsPage.tsx:153-183`), downscales any raster image to
max 1600px / JPEG q0.78 before upload (PDFs pass through untouched). This runs at submit time, not at
extraction time.

### 3.4 Validation

- **Server-side**: global `ValidationPipe({ whitelist: true, transform: true })` (`main.ts:70`) enforces the DTOs
  (`class-validator` decorators) on every request — extra fields are stripped, required/typed fields are checked
  before the controller method even runs. Full field list in §7.
- **Client-side**: `validateForm()` (`UploadBillsPage.tsx:668-711`) checks required top-level fields
  (`vehicle_number`, `category_id`, `invoice_date`, `grand_total`, `vendor_id`), required line-item fields
  (`description`, `unit_rate`, `total_amount`, `uom` must be in the loaded enum list), and, in multi-vehicle mode,
  that each vehicle group's line items sum to that group's total within `0.01` tolerance.
- **No amount-threshold / approval-trigger validation** exists anywhere (no "flag bills over ₹X" logic).

### 3.5 Database Insertion

All writes happen inside `insertEventWithLineItems()` (`maintenance.service.ts:545-636`), run inside a single
`REPEATABLE READ` transaction (per single or batch event):

1. `INSERT INTO maintenance_events (...) RETURNING event_id`.
2. Any line-item `part_no` not already in `parts_master` is bulk-inserted (`ON CONFLICT (part_no) DO NOTHING`).
3. Bulk `INSERT INTO maintenance_line_items`.
4. `UPDATE vehicles SET current_odometer = $odo WHERE vehicle_number = $veh AND current_odometer < $odo` — the
   vehicle's odometer only ever moves forward, and only if this bill's reading is higher.

`createEventsBatch()` (`maintenance.service.ts:690-724`) wraps **all** events from a multi-vehicle split in one
shared transaction — if any one fails (e.g. a duplicate invoice for one of the vehicles), the whole batch rolls
back and **nothing** is saved.

### 3.6 Approval / Verification

**There is no approval/verification workflow.** Whoever has `maintenance.canCreate` permission (or is Admin) can
insert a fully-live event with one API call — no draft state, no reviewer, no status transitions. The only
status-like field is `payment_status` (`UNPAID`/`PARTIAL`/`PAID`), which tracks whether the bill has been paid,
not whether it's been reviewed/approved. (Contrast with `issues`, which does have a real status lifecycle — but
that's a different table.) If the product needs a "pending review" gate before a WhatsApp-sourced bill counts as
real, **that is new functionality that does not exist today** — see §10.

### 3.7 Reports / Downstream Usage

Once inserted, a bill immediately appears in:

- **Month-Wise ledger** (`MaintenanceMonthWisePage.tsx`, route `/maintenance/month-wise`) — expandable
  event/line-item list, editable via `EditEventSheet`.
- **Vehicle-Wise** (`MaintenanceVehicleWisePage.tsx`) — per-vehicle spend, CPK (cost-per-km, via
  `getMaintenanceTripSummary` against `vehicle_trip_yearly_summary`), category breakdown.
- **Category-Wise** (`MaintenanceCategoryWisePage.tsx`) — spend grouped by category.
- **Vendor-Wise** (`MaintenanceVendorWisePage.tsx`) — spend grouped by vendor.
- **Vehicle detail page's Maintenance tab** (`VehicleMaintenanceTab.tsx`) — per-vehicle inline view + add/edit.
- **Tyres module** (`TyresPage.tsx` / `VehicleTyreTab.tsx`) — if any line item has a `tyre_condition`, the event
  is classified as a tyre purchase and shown there instead of the general ledgers (§10).

All three "-wise" pages are **read-only analytics** — no create/update/delete path exists on them.

---

## 4. Backend APIs & Services

### 4.1 Module layout

```
twilight-fleetzen-backend/src/
├── maintenance/                        Layer 2 — MaintenanceService (the actual logic + DB access)
│   ├── maintenance.service.ts          1403 lines — all SQL, Gemini call, issues methods
│   ├── maintenance.module.ts           exports MaintenanceService; NO controller registered here
│   ├── maintenance.controller.ts       ⚠️ DEAD CODE — not imported by any module, unreachable
│   └── dto/
│       ├── create-maintenance-event.dto.ts       CreateLineItemDto, CreateMaintenanceEventDto
│       ├── create-maintenance-events-batch.dto.ts CreateMaintenanceEventsBatchDto
│       ├── update-maintenance-event.dto.ts       UpdateMaintenanceEventDto
│       └── create-issue.dto.ts / update-issue.dto.ts
├── upload-bills-page/                  Layer 1 — the live "create a bill" HTTP surface
├── maintenance-month-wise-page/        Layer 1 — reads/lookups/enums + also a valid write path
├── maintenance-category-wise-page/     Layer 1 — read-only analytics
├── maintenance-vehicle-wise-page/      Layer 1 — read-only analytics + CPK
├── maintenance-vendor-wise-page/       Layer 1 — read-only analytics
└── issues-page/                        Layer 1 — issues CRUD, delegates into MaintenanceService
```

> ⚠️ **`maintenance.controller.ts` is not registered anywhere** (`maintenance.module.ts` has no `controllers`
> array, and `app.module.ts` never imports the controller directly). Do **not** integrate against
> `/maintenance/events` or any `/maintenance/*` path — it will 404. All real traffic goes through the
> `upload-bills-page` / `maintenance-*-wise-page` controllers below.

### 4.2 Endpoints

**`upload-bills-page.controller.ts`** — base `/upload-bills-page`, guards `CookieAuthGuard, PermissionsGuard` +
`@RequirePermission('maintenance', 'canRead')` at class level:

| Method + path | Body | Extra guard | Calls |
|---|---|---|---|
| `GET /categories` | — | canRead | `getCategories()` |
| `GET /vendors` | — | canRead | `getVendors()` |
| `GET /vehicles` | — | canRead | `getVehicles()` |
| `POST /events` | `CreateMaintenanceEventDto` | canCreate | `createEvent()` + activity log |
| `POST /events/batch` | `{ events: CreateMaintenanceEventDto[] }` | canCreate | `createEventsBatch()` |
| `PATCH /events/:eventId` | `UpdateMaintenanceEventDto` | canUpdate | `updateEvent()` |
| `POST /signed-upload-url` | `{ bucket, path }` | canCreate | `createSignedUploadUrl()` |
| `POST /extract-bill` | multipart `file` | canCreate | `extractBillJson()` (Gemini) |

**`maintenance-month-wise-page.controller.ts`** — base `/maintenance-month-wise-page`, same guard pattern. Also
a valid write path (`POST /events`, `PATCH /events/:eventId`, `DELETE /events/:eventId`), plus:
`GET /events?start_date&end_date&payment_status`, `GET /events/:eventId/line-items`,
`GET /parts/last-changed?vehicle_number&invoice_date&part_nos`, `GET /categories|vendors|vehicles`,
`GET /enums`, `POST /signed-upload-url`. **The frontend sources most dropdown lookups from this controller, not
`upload-bills-page`.**

**`maintenance-category-wise-page.controller.ts`**, **`maintenance-vehicle-wise-page.controller.ts`**,
**`maintenance-vendor-wise-page.controller.ts`** — all read-only, `GET /events?<dimension>&start_date&end_date`
plus one dropdown endpoint each. All forward straight into `MaintenanceService.getEvents(filters)`.

**`issues-page.controller.ts`** — base `/issues-page`, `@RequirePermission('issues', ...)` (a *different*
permission module than maintenance). Standard issue CRUD; not needed for bill ingestion.

### 4.3 Authentication & Authorization

- **`CookieAuthGuard`** (`twilight-fleetzen-backend/src/auth/guards/cookie-auth.guard.ts`) validates the Supabase
  JWT from an HttpOnly cookie (or a `Bearer` header as fallback) and attaches `user.permissions` via
  `PermissionsService.getEffectiveAll(userId)`.
- **`PermissionsGuard`** + **`@RequirePermission(module, action)`** — every maintenance controller (except the
  dead one) checks a single flat module string `'maintenance'` with `action ∈ {canRead, canCreate, canUpdate,
  canDelete}`. Admin role always passes. **This is the only permission the calling account needs** — there is no
  finer-grained per-page permission enforced by the backend (the frontend's dotted keys like
  `maintenance.upload_bills` are a frontend-only routing concept, not checked server-side).
- **CSRF**: enforced globally as Express middleware (`twilight-fleetzen-backend/src/auth/middleware/csrf.middleware.ts`),
  not a NestJS guard — every non-GET/HEAD/OPTIONS request must carry a matching `XSRF-TOKEN` cookie and
  `X-CSRF-Token` header (double-submit pattern). **This applies to every `POST`/`PATCH`/`DELETE` call an
  automation makes** — see §9 for the exact handshake required.

### 4.4 `MaintenanceService` — key methods

| Method | Line | Purpose |
|---|---|---|
| `getCategories/getVendors/getVehicles` | 332, 341, 350 | dropdown lookups |
| `getEvents(filters)` | 372 | filtered event list with line items aggregated via `json_agg` |
| `getLineItems(eventId)` | 441 | line items for one event |
| `getLastChanged(...)` | 460 | "when was this part last changed on this vehicle" |
| `getEnums()` | 499 | reads `maintenance_enum_values` view |
| `getTripSummary(year, vehicleNumbers?)` | 520 | CPK data |
| `insertEventWithLineItems(runner, dto)` | 545 (private) | the actual transactional insert — see §3.5 |
| `isDuplicateInvoiceError(e)` | 646 (private) | detects the `(vendor_id, invoice_no)` unique-violation |
| `createEvent(dto)` | 662 | single-event create |
| `createEventsBatch(dtos)` | 690 | multi-vehicle batch create, one shared transaction |
| `updateEvent(eventId, dto)` | 724 | partial update; `line_items`, if present, fully replaces existing ones |
| `deleteEvent(eventId)` | 816 | cascades to line items |
| `extractBillJson(file)` | 1324 | Gemini OCR/parse, read-only, no DB write |
| `getShowroomServiceHistory(vehicleNumber)` | 1248 | legacy service-history read |

Issue-related methods (`findAllIssues`, `createIssue`, etc., lines 863–1230) are out of scope for bill ingestion.

### 4.5 Supabase RPCs

**None.** `grep -rn "\.rpc(" ` across the maintenance/upload-bills/issues controllers and services returns no
matches. Everything is raw parameterized SQL via TypeORM's `DataSource`/`QueryRunner`, or direct Supabase
Storage SDK calls (signed URLs only — not RPC).

---

## 5. Frontend Pages & Components

| File | Route | Role |
|---|---|---|
| `src/pages/UploadBillsPage.tsx` | `/maintenance/upload-bills` | **The only page that creates bills.** Manual + JSON-paste + Gemini-OCR-assisted form. |
| `src/pages/MaintenanceMonthWisePage.tsx` | `/maintenance/month-wise` | Ledger + edit (via `EditEventSheet`). No delete UI wired up despite `deleteMaintenanceEvent` being imported. |
| `src/pages/MaintenanceVehicleWisePage.tsx` | `/maintenance/vehicle-wise` | Read-only analytics, per-vehicle spend + CPK. |
| `src/pages/MaintenanceCategoryWisePage.tsx` | `/maintenance/category-wise` | Read-only analytics, per-category spend. |
| `src/pages/MaintenanceVendorWisePage.tsx` | `/maintenance/vendor-wise` | Read-only analytics, per-vendor spend. |
| `src/pages/TyresPage.tsx` / `src/components/maintenance/VehicleTyreTab.tsx` | `/tyres/vehicle-wise` | Tyre-purchase history (a filtered view of the same `maintenance_events` table). |

Components (`src/components/maintenance/`):

- **`EditEventSheet.tsx`** — the module's one "edit an already-saved bill" UI (slide-over form, used by both
  `MaintenanceMonthWisePage` and `VehicleMaintenanceTab`).
- **`VehicleMaintenanceTab.tsx`** — a tab on `VehicleDetailPage`, scoped to one vehicle; has its own inline
  "Add Event" form calling `createMaintenanceEvent` (the Month-Wise endpoint, not `upload-bills-page`).
- **`TyrePurchaseDrawer.tsx`** — dedicated tyre-purchase form; internally still creates a normal
  `maintenance_events` row via the same `createUploadBillsEvent`/upload flow, distinguished only by setting
  `tyre_condition` on line items.

API layer: **`src/api/maintenance.api.ts`** (295 lines, Axios only, never calls Supabase directly). Base path
constants: `MONTH = '/maintenance-month-wise-page'`, `VEHICLE = '/maintenance-vehicle-wise-page'`,
`UPLOAD = '/upload-bills-page'`. Full function list — see the file directly; the ones relevant to bill
ingestion are `createUploadBillsEvent`, `createUploadBillsEventsBatch`, `updateUploadBillsEvent`,
`createUploadBillsSignedUploadUrl`, `extractUploadBillsJson`, `getMaintenanceEnums`.

React Query: only one dedicated hook exists, `useMaintenanceEnums()` (`src/hooks/useMaintenanceEnums.ts`).
Everything else (events, categories, vendors) is called inline per-page with inconsistent query keys across
pages — not relevant to a backend-integrating agent, but worth knowing if you also touch the frontend.

**No automatic link to Payments/Petty Cash.** Creating or updating a maintenance bill never creates a vendor
payment or petty-cash entry. The only shared state is the `vendors` table itself.

---

## 6. Key Functions by Step

| Step | File : Function |
|---|---|
| Trigger upload dialog | `src/pages/UploadBillsPage.tsx` — "Upload Bill" button, `:1189-1193` |
| Pick file | `UploadBillsPage.tsx:1746-1752` (`<input accept=".png,.jpg,.jpeg,.pdf">`) |
| Request signed upload URL | `src/api/maintenance.api.ts:262` `createUploadBillsSignedUploadUrl()` → backend `upload-bills-page.service.ts:49-62` `createSignedUploadUrl()` |
| Compress image before upload | `UploadBillsPage.tsx:153-183` `compressImage()` |
| OCR/parse a bill | `src/api/maintenance.api.ts:285` `extractUploadBillsJson()` → backend `maintenance.service.ts:1324-1402` `extractBillJson()` |
| Map parsed JSON → form | `UploadBillsPage.tsx:261-376` `mapJsonToForm()` |
| Client-side validate | `UploadBillsPage.tsx:668-711` `validateForm()` |
| Submit (single) | `UploadBillsPage.tsx:909-1160` `handleSubmit()` → `createUploadBillsEvent()`/`updateUploadBillsEvent()` |
| Create event (backend, single) | `maintenance.service.ts:662` `createEvent()` |
| Create events (backend, batch) | `maintenance.service.ts:690` `createEventsBatch()` |
| Core transactional insert | `maintenance.service.ts:545` `insertEventWithLineItems()` (private) |
| Duplicate detection | `maintenance.service.ts:646` `isDuplicateInvoiceError()` (private) |
| Update event | `maintenance.service.ts:724` `updateEvent()` |
| Edit UI | `src/components/maintenance/EditEventSheet.tsx` |
| Ledger / reports | `MaintenanceMonthWisePage.tsx`, `MaintenanceVehicleWisePage.tsx`, `MaintenanceCategoryWisePage.tsx`, `MaintenanceVendorWisePage.tsx` |

---

## 7. Business Rules & Validations

1. **Duplicate protection** is `(vendor_id, invoice_no)` UNIQUE on `maintenance_events`, enforced reactively —
   there is **no pre-submit "does this invoice already exist" check**. A repeat insert throws a Postgres
   `23505`, caught and re-thrown as `ConflictException` → HTTP `409`. Callers must catch this rather than assume
   success.
2. **Parts auto-registration**: any `part_no` not already in `parts_master` is silently inserted
   (`ON CONFLICT (part_no) DO NOTHING`) as part of the bill-creation transaction — no separate catalog step.
3. **Odometer only moves forward**: a bill's `odometer_kms`, if present, updates `vehicles.current_odometer`
   only when it's *greater than* the current value. This is application logic (in `insertEventWithLineItems`),
   **not a DB trigger** — anything that writes `maintenance_events` outside this code path must replicate it
   manually to stay consistent.
4. **`item_category`** must be one of `PART | LABOR | LUBRICANT | SUBLET` (DTO `@IsIn`, backed by a DB enum). Note:
   the Gemini extraction prompt's instructions mention `OTHER` as a valid category, but the DTO/DB do **not**
   accept it — an OCR result of `OTHER` must be re-mapped to one of the four real values before submit.
5. **`maintenance_type`**: `SCHEDULED | UNSCHEDULED`. **`payment_status`**: `UNPAID | PARTIAL | PAID`.
6. **Tyre classification is implicit, not a separate table/flag on the event**: setting `tyre_condition` on any
   line item makes the frontend (`isTyreEvent()`, `src/api/maintenance.api.ts:125-127`) treat the whole event as
   a tyre purchase, excluding it from `getMaintenanceEvents()`'s normal result set and routing it to the Tyres
   module instead. **Do not set `tyre_condition` on a regular maintenance bill's line items**, or it will
   disappear from the standard ledger/analytics pages.
7. **Multi-vehicle batch is all-or-nothing**: `createEventsBatch()` uses one shared transaction across every
   event in the batch — if any single event fails validation or hits the duplicate constraint, none of the
   batch is saved.
8. **`vendor_id`/`category_id` must be resolved against existing rows, never invented.** The existing frontend
   deliberately never auto-applies Gemini's extracted `vendor_name`/category guess as a foreign key — a human
   always picks from the real `vendors`/`maintenance_categories` dropdowns. Any automation should do the same
   (fuzzy-match on `vendor_name`/`vendor_gstin` against existing `vendors` rows) to avoid orphaned FKs or
   duplicate vendor rows.
9. **No amount-threshold / approval-trigger rule exists** — there is nothing in the code that flags "large"
   bills for extra review.
10. **No file-size/type validation server-side** — the `.png,.jpg,.jpeg,.pdf` restriction on the upload input is
    client-side/advisory only.
11. **RBAC, not per-record ownership**: any account with `maintenance.canCreate` can create a bill for *any*
    vehicle — there's no "assigned parking incharge for this vehicle" restriction on maintenance writes (unlike,
    e.g., issues, which has location-scoped visibility for Parking Incharges).

---

## 8. Sequence of Operations (Bill Received → Fully Processed)

```
1.  Bill file arrives (photo/PDF)
2.  [optional] POST /upload-bills-page/extract-bill  (multipart file)
       → Gemini parses → returns structured JSON (vendor_name, invoice_no, invoice_date,
         odometer_kms, grand_total, line_items[], possibly vehicle_groups[] for multi-vehicle bills)
3.  Resolve vendor_id  = fuzzy-match parsed vendor_name/GSTIN against GET /maintenance-month-wise-page/vendors
    Resolve category_id = match/select from GET /maintenance-month-wise-page/categories
    Resolve uom values   = must be members of GET /maintenance-month-wise-page/enums → uomOptions
4.  Build CreateMaintenanceEventDto (or CreateMaintenanceEventDto[] for a multi-vehicle bill)
5.  POST /upload-bills-page/signed-upload-url  { bucket: 'maintenance_bills', path }
       → PUT file bytes to the returned signedUrl
6.  POST /upload-bills-page/events            (single vehicle)
    or
    POST /upload-bills-page/events/batch      (multiple vehicles, body { events: [...] })
       → on success: { event_id } / { event_ids: [...] }
       → on 409: invoice+vendor already recorded — treat as "already ingested", do not retry blindly
7.  PATCH /upload-bills-page/events/:eventId  { bill_image_url: publicUrl }
       → attaches the uploaded receipt to the created event
8.  Done. The bill is immediately live — visible in Month-Wise, Vehicle-Wise, Category-Wise,
    Vendor-Wise pages, and (if odometer_kms was higher) has already bumped vehicles.current_odometer.
    No further "approval" step exists to wait for.
```

All of steps 3–7 (except the extraction call) require an authenticated session with a valid CSRF token — see §9.

---
