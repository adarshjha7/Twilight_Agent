const fs = require('fs');
const path = require('path');
const config = require('../config');

const IMAGE_KEEP_COUNT = 10;

function getTodayFolder(subDir) {
  const today = new Date().toISOString().slice(0, 10);
  const dir = path.join(config.storage.dir, subDir, today);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/**
 * Save a Buffer to disk under storage/<subDir>/YYYY-MM-DD/<filename>
 * Returns the absolute path of the saved file.
 */
function saveFile(buffer, filename, subDir = 'raw') {
  const dir = getTodayFolder(subDir);
  const filePath = path.join(dir, filename);
  fs.writeFileSync(filePath, buffer);
  return filePath;
}

/**
 * Save an incoming image to storage/images/received/ — the processing queue.
 * Files wait here until processed, then move to images/processed/.
 * Returns the absolute path of the saved file.
 */
function saveImage(buffer, filename) {
  const dir = path.join(config.storage.dir, 'images', 'received');
  fs.mkdirSync(dir, { recursive: true });
  const filePath = path.join(dir, filename);
  fs.writeFileSync(filePath, buffer);
  return filePath;
}

/**
 * Move a processed image from images/received/ to images/processed/.
 * Keeps only the most recent IMAGE_KEEP_COUNT files in processed/.
 * Returns the new absolute path.
 */
function moveImageToProcessed(filePath) {
  const dir = path.join(config.storage.dir, 'images', 'processed');
  fs.mkdirSync(dir, { recursive: true });
  const dest = path.join(dir, path.basename(filePath));
  fs.renameSync(filePath, dest);

  // Prune old processed images
  const files = fs.readdirSync(dir)
    .filter((f) => fs.statSync(path.join(dir, f)).isFile())
    .map((f) => ({ name: f, mtime: fs.statSync(path.join(dir, f)).mtimeMs }))
    .sort((a, b) => b.mtime - a.mtime);
  files.slice(IMAGE_KEEP_COUNT).forEach(({ name }) => {
    try { fs.unlinkSync(path.join(dir, name)); } catch (e) { /* locked file — prune next time */ }
  });

  return dest;
}

/**
 * Save a JSON-serialisable object to storage/processed/YYYY-MM-DD/<filename>.json
 */
function saveProcessed(data, filename) {
  const dir = getTodayFolder('processed');
  const filePath = path.join(dir, `${filename}.json`);
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2));
  return filePath;
}

const DATE_FOLDER_RE = /^\d{4}-\d{2}-\d{2}$/;

function isOlderThanRetention(dateStr, retentionDays) {
  const cutoff = Date.now() - retentionDays * 24 * 60 * 60 * 1000;
  return new Date(`${dateStr}T00:00:00Z`).getTime() < cutoff;
}

/**
 * Delete date-stamped folders (storage/raw/YYYY-MM-DD, storage/processed/YYYY-MM-DD)
 * and queued files in storage/images/received/ older than config.storage.retentionDays.
 */
function cleanupOldData() {
  const retentionDays = config.storage.retentionDays;

  ['raw', 'processed'].forEach((subDir) => {
    const dir = path.join(config.storage.dir, subDir);
    if (!fs.existsSync(dir)) return;
    fs.readdirSync(dir).forEach((name) => {
      if (!DATE_FOLDER_RE.test(name)) return;
      if (!isOlderThanRetention(name, retentionDays)) return;
      fs.rmSync(path.join(dir, name), { recursive: true, force: true });
    });
  });

  const receivedDir = path.join(config.storage.dir, 'images', 'received');
  if (fs.existsSync(receivedDir)) {
    const cutoff = Date.now() - retentionDays * 24 * 60 * 60 * 1000;
    fs.readdirSync(receivedDir).forEach((name) => {
      const filePath = path.join(receivedDir, name);
      if (!fs.statSync(filePath).isFile()) return;
      if (fs.statSync(filePath).mtimeMs < cutoff) {
        try { fs.unlinkSync(filePath); } catch (e) { /* locked file — prune next time */ }
      }
    });
  }
}

module.exports = { saveFile, saveImage, saveProcessed, moveImageToProcessed, cleanupOldData };
