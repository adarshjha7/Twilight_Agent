const winston = require('winston');
const path = require('path');
const fs = require('fs');
const config = require('../config');

fs.mkdirSync(config.logging.dir, { recursive: true });

const logger = winston.createLogger({
  // .env shares LOG_LEVEL with the Python agent, whose loguru wants uppercase
  // ("INFO"); winston only accepts lowercase and silently logs NOTHING on an
  // unknown level — normalize here.
  level: (config.logging.level || 'info').toLowerCase(),
  format: winston.format.combine(
    winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
    winston.format.errors({ stack: true }),
    winston.format.printf(({ timestamp, level, message, stack }) =>
      stack
        ? `${timestamp} [${level.toUpperCase()}] ${message}\n${stack}`
        : `${timestamp} [${level.toUpperCase()}] ${message}`
    )
  ),
  transports: [
    new winston.transports.Console({
      format: winston.format.combine(
        winston.format.colorize(),
        winston.format.printf(({ timestamp, level, message }) =>
          `${timestamp} [${level}] ${message}`
        )
      ),
    }),
    // Separate filenames from the Python agent's loguru files (agent.log/error.log) —
    // two processes writing the same file on Windows causes EPERM locks and lost lines.
    new winston.transports.File({
      filename: path.join(config.logging.dir, 'gateway.log'),
      maxsize: 10 * 1024 * 1024, // 10 MB
      maxFiles: 5,
    }),
    new winston.transports.File({
      filename: path.join(config.logging.dir, 'gateway-error.log'),
      level: 'error',
    }),
  ],
});

module.exports = logger;
