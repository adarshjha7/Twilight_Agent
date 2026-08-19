const http = require('http');
const logger = require('./utils/logger');

// Manual-run entry point for cron jobs living in this same process (e.g. the
// inspection scheduler) — lets a job be fired on demand to verify behavior
// against real data without waiting for its cron time. Bound to 127.0.0.1
// only: internal use, same trust boundary as the Python agent's API.
//
// Usage: curl "http://localhost:<port>/trigger?job=post-trip"
function startAdminServer(jobs, port) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname !== '/trigger') {
      res.writeHead(404).end('Not found');
      return;
    }

    const jobName = url.searchParams.get('job');
    const runner = jobs[jobName];
    if (!runner) {
      res.writeHead(400).end(`Unknown job "${jobName}". Available: ${Object.keys(jobs).join(', ')}`);
      return;
    }

    res.writeHead(202).end(`Triggered "${jobName}" — check logs for the result\n`);
    runner().catch((err) => logger.error(`[AdminServer] Manual run of "${jobName}" failed: ${err.message}`));
  });

  server.listen(port, '127.0.0.1', () => {
    logger.info(`[AdminServer] Manual trigger endpoint listening on 127.0.0.1:${port} (jobs: ${Object.keys(jobs).join(', ')})`);
  });

  return server;
}

module.exports = { startAdminServer };
