const axios = require('axios');
const path = require('path');
const { extractMedia } = require('../media/extractor');
const { moveImageToProcessed } = require('../utils/storage');
const logger = require('../utils/logger');
const config = require('../config');

const MEDIA_TYPES = new Set(['image', 'document']);

// ── FIFO processing queue ─────────────────────────────────────────────────────
// Downloads land in storage/images/received/. Jobs are processed strictly one
// at a time; each file moves to storage/images/processed/ when done.
const queue = [];
let draining = false;

function enqueue(job) {
  queue.push(job);
  console.log(`[QUEUE] Added ${path.basename(job.filePath)} — ${queue.length} in queue`);
  drainQueue();
}

async function drainQueue() {
  if (draining) return;
  draining = true;
  while (queue.length > 0) {
    const job = queue.shift();
    await processJob(job);
  }
  draining = false;
  console.log('[QUEUE] Empty — waiting for new files');
}

async function processJob(job) {
  const { message, chatName, filePath, mediaType } = job;
  console.log(`[QUEUE] Processing ${path.basename(filePath)} (${queue.length} waiting)`);

  try {
    const response = await axios.post(`${config.agentService.url}/process`, {
      file_path: filePath,
      media_type: mediaType,
      message_id: message.id.id,
      caption: message.body || '',
      chat_name: chatName,
    }, { timeout: config.agentService.timeoutMs });

    const outcome = response.data;

    if (outcome.success) {
      logger.info(`[WA] Done — vendor: ${outcome.result?.vendor_name}`);
      console.log('\n========== EXTRACTED PETTY CASH JSON ==========');
      console.log(JSON.stringify(outcome.result, null, 2));
      console.log('================================================\n');
      const handled = await sendPettyCashFeedback(message, outcome.result);
      if (!handled) await message.react('✅').catch(() => {});
    } else {
      logger.warn(`[WA] Agent could not process: ${outcome.reason}`);
      await message.react('❓').catch(() => {});
    }
  } catch (err) {
    const detail = err.response ? JSON.stringify(err.response.data) : err.message;
    logger.error(`[WA] Pipeline error: ${detail}`);
    await message.react('❌').catch(() => {});
  } finally {
    if (mediaType === 'image') {
      try {
        const dest = moveImageToProcessed(filePath);
        console.log(`[QUEUE] Moved to processed: ${path.basename(dest)}`);
      } catch (err) {
        console.log(`[QUEUE] Could not move file: ${err.message}`);
      }
    }
  }
}

function isMonitored(chatName) {
  return config.whatsapp.monitoredChats.some(
    (monitored) => chatName.toLowerCase().includes(monitored.toLowerCase())
  );
}

// ── Own-reply guard ───────────────────────────────────────────────────────────
// message_create fires for messages this account sends, including the bot's own
// confirmations. A confirmation like "✅ Opening balance set! Month: June,
// Amount: ₹5,000" contains a month and amount, so the LLM selects
// set_opening_balance again and the bot answers its own answer forever.
// Track the ids of replies we send so they are never re-processed.
const ownReplyIds = new Set();

// Invisible zero-width space prepended to every bot reply. Lets us recognise
// our own replies by content even in the race where message_create fires
// before reply() resolves with the id — without blocking the owner's normal
// quoted-reply messages.
const BOT_MARKER = String.fromCharCode(0x200b);

async function sendReply(message, text) {
  try {
    const sent = await message.reply(BOT_MARKER + text);
    const id = sent?.id?.id;
    if (id) {
      ownReplyIds.add(id);
      setTimeout(() => ownReplyIds.delete(id), 60000);
    }
  } catch (err) {
    logger.warn(`[WA] Could not send reply: ${err.message}`);
  }
}

// Reacts + replies (quoted, so it targets the specific screenshot) based on the
// petty cash DB status the agent returned. Returns true if feedback was sent.
async function sendPettyCashFeedback(message, result) {
  const dbStatus = result?._db;
  if (!dbStatus) return false;

  if (dbStatus.missing?.length) {
    await message.react('❌').catch(() => {});
    await sendReply(
      message,
      `❌ *Entry NOT saved — could not read:*\n${dbStatus.missing.map((m) => `  • ${m}`).join('\n')}\n\n` +
      `Please resend a screenshot where these details are clearly visible.`
    );
    return true;
  }
  if (dbStatus.unknown_bank) {
    await message.react('❌').catch(() => {});
    const { last4, bank_options } = dbStatus.unknown_bank;
    const acct = last4 ? `The account ending *${last4}*` : 'The account on this receipt';
    await sendReply(
      message,
      `❌ *Entry NOT saved — could not identify the bank.*\n` +
      `${acct} is not a recognised account for this group.\n\n` +
      `Recognised accounts:\n${(bank_options || []).map((b) => `  • ${b}`).join('\n')}\n\n` +
      `Please confirm which bank this payment was made from and resend.`
    );
    return true;
  }
  if (dbStatus.saved) {
    await message.react('✅').catch(() => {});
    const amount = Math.abs(Number(result?.amount) || 0);
    await sendReply(
      message,
      `✅ *Petty cash entry saved!*\n🧾 Ref: ${dbStatus.transaction_ref}\n💰 Amount: ₹${amount.toLocaleString('en-IN')}`
    );
    return true;
  }
  if (dbStatus.duplicate) {
    await message.react('⚠️').catch(() => {});
    await sendReply(
      message,
      `⚠️ This transaction is already recorded (${dbStatus.existing_ref}) — skipped duplicate.`
    );
    return true;
  }
  return false;
}

async function handleMessage(message) {
  const chat = await message.getChat();
  const chatName = chat.name || chat.id.user;

  console.log(`[MSG] type=${message.type} | group="${chatName}" | fromMe=${message.fromMe} | id=${message.id.id}`);

  // Skip the bot's own replies, recognised by id or by the invisible marker
  // sendReply() prepends. Everything else from this account still passes —
  // including the owner's fresh commands AND swipe-to-reply quoted commands.
  if (message.fromMe && (ownReplyIds.has(message.id.id) || (message.body || '').startsWith(BOT_MARKER))) {
    console.log(`[SKIP] Own reply: ${message.id.id}`);
    return;
  }

  if (!isMonitored(chatName)) {
    console.log(`[SKIP] Not monitored: "${chatName}"`);
    return;
  }

  // ── Text messages: forward to agent for ReAct tool selection ──────────────
  if (message.type === 'chat') {
    const text = (message.body || '').trim();
    if (!text) return;

    logger.info(`[WA] Text message from "${chatName}": ${text}`);

    try {
      const response = await axios.post(`${config.agentService.url}/process`, {
        media_type: 'text',
        message_id: message.id.id,
        caption: text,
        chat_name: chatName,
      }, { timeout: config.agentService.timeoutMs });

      const outcome = response.data;
      const reply = outcome.result?.reply;

      if (reply) {
        await sendReply(message, reply);
      } else {
        // Petty cash entries extracted from text have no reply field —
        // report their DB save status the same way as screenshots.
        await sendPettyCashFeedback(message, outcome.result);
      }

      if (outcome.success) {
        logger.info(`[WA] Text processed by tool: ${outcome.tool}`);
      } else {
        logger.warn(`[WA] Agent could not handle text: ${outcome.reason}`);
      }
    } catch (err) {
      const detail = err.response ? JSON.stringify(err.response.data) : err.message;
      logger.error(`[WA] Text pipeline error: ${detail}`);
      await message.react('❌').catch(() => {});
      await sendReply(message, '⚠️ Something went wrong while processing this — please try again in a minute.');
    }
    return;
  }

  // ── Media messages: existing image/document pipeline ──────────────────────
  if (!MEDIA_TYPES.has(message.type)) {
    console.log(`[SKIP] Unhandled message type: ${message.type}`);
    return;
  }

  console.log(`[WA] Downloading ${message.type} from "${chatName}" — id: ${message.id.id}`);
  logger.info(`[WA] Downloading ${message.type} from "${chatName}" — id: ${message.id.id}`);

  try {
    // Download into storage/images/received/, then enqueue — processing is
    // strictly sequential so multiple files never race each other.
    const mediaResult = await extractMedia(message);
    if (!mediaResult) return;

    enqueue({
      message,
      chatName,
      filePath: mediaResult.filePath,
      mediaType: mediaResult.mediaType,
    });
  } catch (err) {
    logger.error(`[WA] Download error: ${err.message}`);
    await message.react('❌').catch(() => {});
  }
}

module.exports = { handleMessage };
