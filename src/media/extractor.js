const path = require('path');
const { saveFile, saveImage } = require('../utils/storage');
const logger = require('../utils/logger');

const SUPPORTED_IMAGE_MIMES = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif']);
const SUPPORTED_DOC_MIMES = new Set(['application/pdf']);

/**
 * Downloads media from a whatsapp-web.js Message and saves it to disk.
 * Returns { filePath, mimeType, mediaType } or null if not supported.
 */
async function extractMedia(message) {
  const mime = message.type === 'document'
    ? message.mimetype
    : (message._data && message._data.mimetype) || message.mimetype;

  const isImage = SUPPORTED_IMAGE_MIMES.has(mime);
  const isPdf = SUPPORTED_DOC_MIMES.has(mime);

  if (!isImage && !isPdf) {
    console.log(`[MEDIA] Skipping unsupported mime type: ${mime}`);
    return null;
  }

  // Self-sent messages fire message_create before the upload finishes — the
  // message object is a stale snapshot with hasMedia=false and never updates
  // itself. Waiting alone doesn't help; the message must be RELOADED from
  // WhatsApp to get the synced media reference.
  if (!message.hasMedia) {
    console.log('[MEDIA] hasMedia=false — waiting 5s then reloading message...');
    await new Promise((r) => setTimeout(r, 5000));
    try {
      const fresh = await message.reload();
      if (fresh) message = fresh;
    } catch (err) {
      console.log(`[MEDIA] reload failed: ${err.message}`);
    }
    console.log(`[MEDIA] After reload — hasMedia=${message.hasMedia}`);
  }

  let media;
  try {
    console.log(`[MEDIA] Downloading (${mime})... hasMedia=${message.hasMedia}`);
    media = await message.downloadMedia();
  } catch (err) {
    console.log(`[MEDIA] Download FAILED: ${err.message}`);
    logger.error(`Failed to download media: ${err.message}`);
    return null;
  }

  if (!media || !media.data || media.data.length === 0) {
    console.log('[MEDIA] Download returned no data');
    logger.warn('Downloaded media has no data');
    return null;
  }
  console.log(`[MEDIA] Download complete — ${media.data.length} b64 chars`);

  const buffer = Buffer.from(media.data, 'base64');
  const ext = isImage ? mime.split('/')[1] : 'pdf';
  // Include message id — Date.now() alone collides when multiple files arrive in the same ms
  const messageId = (message.id && message.id.id) || '';
  const filename = `${Date.now()}_${messageId}.${ext}`;
  const filePath = isImage
    ? saveImage(buffer, filename)
    : saveFile(buffer, filename, 'pdfs');

  logger.info(`Saved ${isImage ? 'image' : 'PDF'} → ${filePath}`);
  return { filePath, mimeType: mime, mediaType: isImage ? 'image' : 'pdf' };
}

module.exports = { extractMedia };
