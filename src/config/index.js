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
};

module.exports = config;
