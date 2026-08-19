Implement an automated inspection scheduler in this project. This project
already runs a live WhatsApp gateway (Baileys) for other automations
(e.g. a maintenance scheduler) — the inspection scheduler must be added
as an ADDITIONAL feature inside that SAME running process, not a new
service.

## Hard constraint — do not break the existing WhatsApp connection
- Do NOT call makeWASocket() / start a new Baileys session anywhere.
  Do NOT create or reuse a second baileys_auth folder. There must be
  exactly one WhatsApp connection in this project, before and after
  this change.
- Find wherever the existing socket is held (likely something like
  src/whatsapp/client.js exporting a send/reply helper) and add a new
  helper that reuses that SAME live socket to send a message to a
  WhatsApp group by name — do not open a new connection to do this.
- Find wherever the existing scheduler/cron logic lives (the current
  maintenance scheduler) and register the new cron jobs the same way
  — same process, same startup file, same logging setup. Do not spin
  up a second entrypoint/process for this.
- Do not modify the existing maintenance-scheduler logic, its cron
  timings, or any other existing message-handling flow. This is a
  pure addition — run the existing test/verification steps for the
  current features after this change and confirm nothing else changed
  behavior.

## Data source
Table: `inspection_submissions` in the same Supabase project already
configured for this app (reuse the existing Supabase client — do not
create a second one).

Sample row shape:
{"idx":90,"submission_id":"0ebe0671-abc5-4831-95ea-e889c8acf47a",
"vehicle_number":"TG13T3621","trip_id":"1546_20260803",
"form_version_id":"pre-trip-base-hyd-inspection-v3","status":"Completed",
"completed_by":"arun_mukkoti","completed_at":"2026-08-03 13:43:47.73+00",
"meter_reading":null,"public_upload_token":null,
"public_upload_token_expires_at":null,
"public_upload_token_generated_by":null,"pending_photo_urls":[]}

There is no dedicated "scheduled date" or "inspection type" column.
Derive both from existing fields:
- date  → the `<tripNumber>_<YYYYMMDD>` suffix on `trip_id`
          (e.g. "1546_20260803" → 2026-08-03)
- type  → substring "pre-trip" / "post-trip" in `form_version_id`
          (case-insensitive)

## Schedule (two daily jobs, no LLM/Gemini involved — pure DB read +
## WhatsApp post)
1. 12:00 PM daily → find inspection_submissions where status =
   "Scheduled" AND type = Post-Trip AND trip date = YESTERDAY
   (relative to the job's run date). Trigger those.
2. 9:30 PM daily → find inspection_submissions where status =
   "Scheduled" AND type = Pre-Trip AND trip date = TODAY. Trigger those.
Both jobs run on a fixed IANA timezone (default Asia/Kolkata, make it
configurable via env var).

## "Trigger" = post a WhatsApp summary
Triggering means posting ONE summary message (vehicle_number + trip_id
per line, plus a count/date header) to a specific WhatsApp group,
resolved by name (configurable via env var, substring match against
groups the bot is already in — do not require a new QR scan or new
group join flow).

Do NOT write anything back to inspection_submissions (no status
column update) — this table is read-only for this feature.

## Exactly-once guarantee (no DB write, per above)
Maintain a small local persisted store (e.g. a JSON file under this
project's existing storage/ or data directory — match whatever
convention already exists in this repo) that records submission_ids
which have already been successfully posted. Before each job sends,
filter out ids already in that store. After a successful WhatsApp
send, add the newly-sent ids to the store. This must survive process
restarts. If the WhatsApp send fails, do NOT mark those ids as sent
(so a manual re-run the same day can retry them).

## Logging (use this project's existing logger, same log files/
## conventions as the current maintenance scheduler)
For every run of every job, log:
- number of Scheduled submissions found for that window
- number already triggered previously (skipped)
- number newly triggered successfully
- number of failures (with the error reason), and note that failed
  ones remain eligible for retry (not marked as sent)

## Testing
Add a manual-run entry point (mirroring however this project already
supports ad-hoc/manual invocation of jobs, or a small script) to fire
either job immediately without waiting for the cron time, so behavior
can be verified against real data before relying on the schedule.
After implementing, run it manually once for each job against
whatever Scheduled rows currently exist and confirm: correct rows
selected, one WhatsApp message posted to the right group, log lines
match the four categories above, and a second manual run does not
re-post the same submissions.

## Reference implementation
A working version of this exact feature (same schema, same job
timing, same dedupe design, same log categories) already exists at
`../Skipped Inspection reminder agent/src/scheduler/inspectionScheduler.js`
and the `sendToGroupByName` addition in that project's
`src/whatsapp/client.js`. Use it as a design reference for the query/
date-derivation/dedupe/logging logic, but adapt file names, the
Supabase client import, the logger, and the WhatsApp-send helper to
match THIS project's actual existing modules — do not copy it
verbatim if this project's structure differs.
