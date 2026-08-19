const cron = require('node-cron');
const { supabase } = require('../integrations/supabase/client');
const { sendToGroupByName } = require('../whatsapp/client');
const logger = require('../utils/logger');
const config = require('../config');
const sentStore = require('./sentStore');

// ─── Date/type derivation ──────────────────────────────────────────────────
// There is no dedicated "scheduled date" or "inspection type" column on
// inspection_submissions — both are derived from existing fields (see
// inspection_reminder_agent.md).

/** "YYYY-MM-DD" for today (offsetDays=0) or a day offset from it, evaluated
 *  in the given IANA timezone regardless of the server's own local time. */
function dateStrInTz(offsetDays, tz) {
  const todayStr = new Intl.DateTimeFormat('en-CA', { timeZone: tz }).format(new Date()); // YYYY-MM-DD
  if (offsetDays === 0) return todayStr;
  const d = new Date(`${todayStr}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

// trip_id shape: "<tripNumber>_<YYYYMMDD>" e.g. "1546_20260803" → 2026-08-03
function deriveDateFromTripId(tripId) {
  const m = /_(\d{8})$/.exec(tripId || '');
  if (!m) return null;
  const s = m[1];
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

// "2026-08-04" → "August 4, 2026" — message display only, never used for
// matching (deriveDateFromTripId/dateStrInTz stay in YYYY-MM-DD for that).
const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function formatDisplayDate(isoDate) {
  const [y, m, d] = isoDate.split('-').map(Number);
  return `${MONTH_NAMES[m - 1]} ${d}, ${y}`;
}

// form_version_id shape: "pre-trip-base-hyd-inspection-v3" (case-insensitive
// substring match for "pre-trip" / "post-trip")
function deriveType(formVersionId) {
  const s = (formVersionId || '').toLowerCase();
  if (s.includes('post-trip')) return 'Post-Trip';
  if (s.includes('pre-trip')) return 'Pre-Trip';
  return null;
}

// ─── One job run ────────────────────────────────────────────────────────────

/**
 * Finds Scheduled submissions of `type` for the trip date at `offsetDays`
 * from today (in config.inspection.timezone), posts ONE summary message to
 * the configured WhatsApp group for any not already sent, and records the
 * ids as sent only on a successful send (so a failed send stays retryable).
 */
async function runJob(jobName, { type, offsetDays }) {
  const tz = config.inspection.timezone;
  const targetDate = dateStrInTz(offsetDays, tz);
  const targetDateCompact = targetDate.replace(/-/g, '');

  let rows;
  try {
    // Push both filters to the DB (status + LIKE on the derived substrings)
    // to avoid pulling the whole table — deriveType/deriveDateFromTripId
    // below then re-check exactly, since LIKE is only an approximation.
    const { data, error } = await supabase
      .from('inspection_submissions')
      .select('submission_id, vehicle_number, trip_id, form_version_id, status')
      .eq('status', 'Scheduled')
      .ilike('form_version_id', `%${type.toLowerCase()}%`)
      .like('trip_id', `%_${targetDateCompact}`);
    if (error) throw error;
    rows = data || [];
  } catch (err) {
    logger.error(`[InspectionScheduler] ${jobName} — query failed: ${err.message}`);
    return;
  }

  const matched = rows.filter(
    (r) => deriveType(r.form_version_id) === type && deriveDateFromTripId(r.trip_id) === targetDate
  );

  const alreadySent = sentStore.load();
  const toSend = matched.filter((r) => !alreadySent.has(r.submission_id));
  const skipped = matched.length - toSend.length;

  if (toSend.length === 0) {
    logger.info(
      `[InspectionScheduler] ${jobName} (${type}, ${targetDate}) — found=${matched.length} skipped=${skipped} newly_triggered=0 failures=0`
    );
    return;
  }

  // Post-Trip runs at noon for YESTERDAY's trip date (vehicle reaches base
  // the next morning, so the pending post-trip form is still dated the prior
  // evening) — "Morning Pending Inspections". Pre-Trip runs at 9:30 PM for
  // TODAY's trip date — "Evening Pending Inspections".
  const header = type === 'Post-Trip' ? 'Morning Pending Inspections' : 'Evening Pending Inspections';
  const lines = toSend.map((r) => r.vehicle_number);
  const mentionTags = config.inspection.mentionNumbers.map((n) => `@${n}`).join(' ');
  const message = `${header}: ${formatDisplayDate(targetDate)} (${toSend.length} pending)\n${lines.join('\n')}${mentionTags ? `\n\n${mentionTags}` : ''}`;

  try {
    await sendToGroupByName(config.inspection.groupName, message, config.inspection.mentionNumbers);
    sentStore.markSent(toSend.map((r) => r.submission_id));
    logger.info(
      `[InspectionScheduler] ${jobName} (${type}, ${targetDate}) — found=${matched.length} skipped=${skipped} newly_triggered=${toSend.length} failures=0`
    );
  } catch (err) {
    logger.error(
      `[InspectionScheduler] ${jobName} (${type}, ${targetDate}) — found=${matched.length} skipped=${skipped} newly_triggered=0 failures=${toSend.length} reason="${err.message}" (not marked sent — eligible for retry)`
    );
  }
}

// ─── Cron registration ──────────────────────────────────────────────────────

function schedule() {
  if (!config.inspection.groupName) {
    logger.error('[InspectionScheduler] INSPECTION_WA_GROUP not set — inspection reminders disabled');
    return;
  }

  const tz = config.inspection.timezone;

  // 12:00 PM daily — Post-Trip submissions for YESTERDAY
  cron.schedule('0 12 * * *', () => runJob('post-trip-noon', { type: 'Post-Trip', offsetDays: -1 }), { timezone: tz });

  // 9:30 PM daily — Pre-Trip submissions for TODAY
  cron.schedule('30 21 * * *', () => runJob('pre-trip-night', { type: 'Pre-Trip', offsetDays: 0 }), { timezone: tz });

  logger.info(`[InspectionScheduler] Registered — Post-Trip @ 12:00, Pre-Trip @ 21:30 (${tz}), group="${config.inspection.groupName}"`);
}

module.exports = { schedule, runJob };
