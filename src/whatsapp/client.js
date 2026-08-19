const makeWASocket = require('@whiskeysockets/baileys').default;
const {
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const pino = require('pino');
const logger = require('../utils/logger');

const AUTH_DIR = './baileys_auth';

// Baileys' internal logging is very chatty — keep it silent, we log ourselves.
const waLogger = pino({ level: 'silent' });

// The live socket. Reconnects create a NEW socket; messages already queued
// must not keep using the dead one, so every action (reply/react/download)
// resolves the socket through this reference at call time.
let currentSock = null;

// group jid → subject, so we don't refetch metadata on every message
const groupNames = new Map();

async function resolveChatName(jid) {
  if (!jid.endsWith('@g.us')) return jid.split('@')[0];
  if (groupNames.has(jid)) return groupNames.get(jid);
  try {
    const meta = await currentSock.groupMetadata(jid);
    groupNames.set(jid, meta.subject);
    return meta.subject;
  } catch (err) {
    logger.warn(`Could not fetch group metadata for ${jid}: ${err.message}`);
    return jid;
  }
}

// Disappearing-message / view-once / captioned-document wrappers nest the real
// content one level down — unwrap until we reach it.
function unwrapContent(message) {
  let content = message;
  while (
    content?.ephemeralMessage ||
    content?.viewOnceMessage ||
    content?.viewOnceMessageV2 ||
    content?.documentWithCaptionMessage
  ) {
    content = (
      content.ephemeralMessage ||
      content.viewOnceMessage ||
      content.viewOnceMessageV2 ||
      content.documentWithCaptionMessage
    ).message;
  }
  return content || {};
}

// Wraps a raw Baileys message in the whatsapp-web.js-shaped interface that
// messageHandler.js and media/extractor.js were written against, so the
// business logic is untouched by the library switch.
function adaptMessage(raw) {
  const content = unwrapContent(raw.message);
  const jid = raw.key.remoteJid;

  let type = 'unknown';
  let body = '';
  let mimetype = null;

  if (content.imageMessage) {
    type = 'image';
    body = content.imageMessage.caption || '';
    mimetype = content.imageMessage.mimetype;
  } else if (content.documentMessage) {
    type = 'document';
    body = content.documentMessage.caption || '';
    mimetype = content.documentMessage.mimetype;
  } else if (content.conversation || content.extendedTextMessage) {
    type = 'chat';
    body = content.conversation || content.extendedTextMessage.text || '';
  } else {
    type = Object.keys(content)[0] || 'unknown';
  }

  return {
    type,
    body,
    mimetype,
    fromMe: !!raw.key.fromMe,
    hasMedia: type === 'image' || type === 'document',
    id: { id: raw.key.id },

    async getChat() {
      return { name: await resolveChatName(jid), id: { user: jid.split('@')[0] } };
    },

    async reply(text) {
      const sent = await currentSock.sendMessage(jid, { text }, { quoted: raw });
      return { id: { id: sent?.key?.id } };
    },

    async react(emoji) {
      await currentSock.sendMessage(jid, { react: { text: emoji, key: raw.key } });
    },

    async downloadMedia() {
      const buffer = await downloadMediaMessage(raw, 'buffer', {}, {
        logger: waLogger,
        reuploadRequest: (...args) => currentSock.updateMediaMessage(...args),
      });
      return { data: buffer.toString('base64'), mimetype };
    },

    // Baileys delivers media keys with the message event itself — the
    // whatsapp-web.js "stale snapshot" reload workaround is not needed.
    async reload() {
      return this;
    },
  };
}

// Resolves a group name (substring, case-insensitive) to a jid by asking the
// LIVE socket for every group the bot currently participates in — no new
// connection, no new auth, just a lookup against the existing session.
async function findGroupJidByName(nameSubstring) {
  if (!currentSock) throw new Error('WhatsApp socket not ready');
  const groups = await currentSock.groupFetchAllParticipating();
  const needle = nameSubstring.toLowerCase();
  const match = Object.values(groups).find((g) => (g.subject || '').toLowerCase().includes(needle));
  return match ? match.id : null;
}

// Sends a plain-text message to a group resolved by name, reusing the SAME
// live socket every other send/reply/react in this module uses — this is
// the only way any other feature in this project should message a group by
// name; it must never call makeWASocket() itself.
//
// mentionNumbers (optional): country-code+number, digits only, e.g.
// "917896890802" — text must separately contain "@<same digits>" for
// WhatsApp to render the highlighted tag; this array is what actually
// triggers the mention/notification, the "@digits" text alone does nothing.
async function sendToGroupByName(nameSubstring, text, mentionNumbers = []) {
  const jid = await findGroupJidByName(nameSubstring);
  if (!jid) throw new Error(`No WhatsApp group found matching "${nameSubstring}"`);
  const mentions = mentionNumbers.map((n) => `${n}@s.whatsapp.net`);
  await currentSock.sendMessage(jid, { text, mentions });
}

async function startWhatsApp(onMessage) {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // The WA Web version baked into the Baileys release goes stale and the
  // server then rejects the handshake with 405 (before ever sending a QR) —
  // always announce the current version instead.
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
    console.log(`Using WhatsApp Web version ${version.join('.')}`);
  } catch (err) {
    logger.warn(`Could not fetch latest WA Web version (${err.message}) — using Baileys default`);
  }

  const sock = makeWASocket({
    version,
    auth: state,
    logger: waLogger,
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });
  currentSock = sock;

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      logger.info('WhatsApp QR code received — scan with your phone:');
      qrcode.generate(qr, { small: true });
      // Also print as a URL you can open in browser
      console.log(`\nOr open this in your browser to scan:\nhttps://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${encodeURIComponent(qr)}\n`);
    }

    if (connection === 'open') {
      console.log('✅ WhatsApp connected and listening for messages');
    }

    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      if (statusCode === DisconnectReason.loggedOut) {
        logger.error(`WhatsApp session logged out — delete ${AUTH_DIR}/ and restart to scan a new QR code.`);
        process.exit(1);
      }
      // console.log as well as winston: this line is the only clue when the
      // handshake is rejected, and it must be visible in the terminal.
      console.log(`WhatsApp disconnected (status ${statusCode}: ${lastDisconnect?.error?.message || 'unknown'}) — reconnecting in 5s...`);
      logger.warn(`WhatsApp disconnected (status ${statusCode}) — reconnecting in 5s...`);
      setTimeout(
        () => startWhatsApp(onMessage).catch((err) => logger.error(`Reconnect failed: ${err.message}`)),
        5000
      );
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return; // ignore history-sync batches
    for (const raw of messages) {
      if (!raw.message) continue; // receipts, protocol updates, etc.
      try {
        await onMessage(adaptMessage(raw));
      } catch (err) {
        logger.error(`Message handling error: ${err.message}`);
      }
    }
  });

  return sock;
}

module.exports = { startWhatsApp, sendToGroupByName };
