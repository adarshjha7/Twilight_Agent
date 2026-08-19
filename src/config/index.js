require('dotenv').config();

function requireEnv(key) {
  const val = process.env[key];
  if (!val) throw new Error(`Missing required env var: ${key}`);
  return val;
}

const config = {
  whatsapp: {
    monitoredChats: requireEnv('WA_MONITORED_CHATS')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  },
  agentService: {
    url: process.env.AGENT_SERVICE_URL || 'http://localhost:8000',
    timeoutMs: parseInt(process.env.AGENT_SERVICE_TIMEOUT_MS || '120000', 10),
  },
  storage: {
    dir: process.env.STORAGE_DIR || './storage',
    retentionDays: parseInt(process.env.STORAGE_RETENTION_DAYS || '3', 10),
  },
  logging: {
    level: process.env.LOG_LEVEL || 'info',
    dir: process.env.LOG_DIR || './logs',
  },
  inspection: {
    // Substring match against groups the bot is already in (see
    // sendToGroupByName in whatsapp/client.js) — not required, so an
    // unset value disables the scheduler without crashing the gateway.
    groupName: process.env.INSPECTION_WA_GROUP || '',
    timezone: process.env.INSPECTION_TIMEZONE || 'Asia/Kolkata',
    adminPort: parseInt(process.env.GATEWAY_ADMIN_PORT || '8091', 10),
    // Comma-separated, country code + number, digits only, no "+"/spaces
    // (e.g. "917896890802") — tagged on every reminder message. Empty/unset
    // means no mentions, not an error.
    mentionNumbers: (process.env.INSPECTION_MENTION_NUMBERS || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  },
};

module.exports = config;
