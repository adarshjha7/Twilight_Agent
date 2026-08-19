const fs = require('fs');
const path = require('path');
const config = require('../config');

// Same STORAGE_DIR convention as utils/storage.js — resolved to absolute so
// it doesn't matter which working directory the process was started from.
const STORE_PATH = path.join(path.resolve(config.storage.dir), 'inspection-reminders', 'sent_ids.json');

/** Every submission_id ever successfully posted, as a Set. Survives restarts. */
function load() {
  try {
    const raw = fs.readFileSync(STORE_PATH, 'utf8');
    return new Set(JSON.parse(raw));
  } catch (err) {
    return new Set(); // first run — no store file yet
  }
}

/** Persist newly-sent ids. Only call this AFTER a successful WhatsApp send. */
function markSent(ids) {
  if (!ids.length) return;
  const current = load();
  ids.forEach((id) => current.add(id));
  fs.mkdirSync(path.dirname(STORE_PATH), { recursive: true });
  fs.writeFileSync(STORE_PATH, JSON.stringify([...current], null, 2));
}

module.exports = { load, markSent };
