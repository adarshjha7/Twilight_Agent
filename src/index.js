require('dotenv').config();

const { startWhatsApp } = require('./whatsapp/client');
const { handleMessage } = require('./whatsapp/messageHandler');
const { cleanupOldData } = require('./utils/storage');
const logger = require('./utils/logger');
const config = require('./config');
const { schedule: scheduleInspectionJobs, runJob: runInspectionJob } = require('./scheduler/inspectionScheduler');
const { startAdminServer } = require('./adminServer');

logger.info('Starting WhatsApp service...');
logger.info(`Monitoring chats  : ${config.whatsapp.monitoredChats.join(', ')}`);
logger.info(`Agent service URL : ${config.agentService.url}`);

const CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000;
function runCleanup() {
  try {
    cleanupOldData();
  } catch (err) {
    logger.error(`Storage cleanup failed: ${err.message}`);
  }
}
runCleanup();
setInterval(runCleanup, CLEANUP_INTERVAL_MS);

// Inspection reminder scheduler — pure addition, reuses the WhatsApp socket
// started below (never opens a second connection). Wrapped so a config/DB
// problem here can never take down the WhatsApp connection itself.
try {
  scheduleInspectionJobs();
  startAdminServer(
    {
      'post-trip': () => runInspectionJob('post-trip-manual', { type: 'Post-Trip', offsetDays: -1 }),
      'pre-trip': () => runInspectionJob('pre-trip-manual', { type: 'Pre-Trip', offsetDays: 0 }),
    },
    config.inspection.adminPort
  );
} catch (err) {
  logger.error(`Inspection scheduler setup failed (WhatsApp connection unaffected): ${err.message}`);
}

// Deduplicate: WhatsApp redelivers recent messages whenever the connection
// bounces, which can be MINUTES after the original — a short time window is
// not enough (a 5s window let a 6-screenshot batch get reprocessed after a
// reconnect). Message ids never repeat, so keep a capped insertion-ordered
// set of everything seen this session and drop the oldest past the cap.
const MAX_SEEN_IDS = 2000;
const seenIds = new Set();
async function deduped(msg) {
  const id = msg.id.id;
  if (seenIds.has(id)) return;
  seenIds.add(id);
  if (seenIds.size > MAX_SEEN_IDS) {
    seenIds.delete(seenIds.values().next().value);
  }
  await handleMessage(msg);
}

function initWithRetry(delay = 5000) {
  startWhatsApp(deduped).catch((err) => {
    logger.error(`Failed to initialise WhatsApp client: ${err.message} — retrying in ${delay / 1000}s`);
    setTimeout(() => initWithRetry(Math.min(delay * 2, 60000)), delay);
  });
}

initWithRetry();

process.on('SIGINT', () => {
  logger.info('Shutting down...');
  process.exit(0);
});

process.on('uncaughtException', (err) => logger.error(`Uncaught exception: ${err.message}`, err));
process.on('unhandledRejection', (reason) => logger.error(`Unhandled rejection: ${reason}`));
