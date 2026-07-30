#!/usr/bin/env bash
# ============================================================================
# Twilight Agent — Hostinger VPS deployment runbook
# ============================================================================
#
# This is NOT meant to be run blindly as `bash deploy.sh` start to finish.
# A few steps need you present and interactive (creating .env, scanning the
# WhatsApp QR code) — read the comments and run each numbered section
# yourself, in order, copy-pasting into your SSH session.
#
# Assumes:
#   - Ubuntu/Debian-based Hostinger VPS (apt-based)
#   - You're SSH'd in as a normal user (e.g. twilight@srv1569539), not root
#   - This is a SHARED server already running other projects (challan-api,
#     bus-scraper, service-mg-versions, etc.) — every step below is scoped to
#     this project's own directory / its own Python venv / its own PM2
#     process names, so nothing here touches or restarts anything else
#     already deployed on this box.
#
# Two long-running processes make up this app, same as local dev:
#   1. Node gateway   — owns the WhatsApp (Baileys) connection
#   2. Python agent   — FastAPI service on 127.0.0.1:8000, does the LLM
#                        extraction + Supabase writes
# Port 8000 is internal only (gateway -> agent on localhost) — nothing here
# needs a public port, a domain, or an nginx/reverse-proxy config.
# ============================================================================


# ── 0. Where this lives ─────────────────────────────────────────────────────
PROJECT_DIR="$HOME/Twilight_Agent"
cd "$PROJECT_DIR" || { echo "Twilight_Agent folder not found at $PROJECT_DIR — see step 1"; exit 1; }


# ── 1. Get the code ──────────────────────────────────────────────────────────
# Your `ls` output shows the folder already exists, so this is most likely
# just a pull. If it's actually empty/not a git repo yet, clone instead:
#   rm -rf "$PROJECT_DIR"   # only if it's genuinely empty/junk — check first!
#   git clone https://github.com/adarshjha7/Twilight_Agent.git "$PROJECT_DIR"
#   cd "$PROJECT_DIR"
git pull origin main


# ── 2. Check Node.js version (Baileys needs Node 18+) ───────────────────────
# This server already runs other Node projects, so Node is probably already
# installed system-wide — just confirm the version rather than reinstalling,
# since upgrading system Node could affect those other projects.
node -v
npm -v
# If it reports below v18 and you need to install Node fresh (e.g. via
# NodeSource), do that as its own careful step — not covered here, since it's
# a shared-server, cross-project decision, not specific to this app.


# ── 3. Install gateway (Node) dependencies ──────────────────────────────────
npm install --omit=dev
# --omit=dev skips nodemon — production doesn't need the file-watcher.


# ── 4. Python — dedicated virtualenv, isolated from other projects' venvs ───
cd "$PROJECT_DIR/agent"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
cd "$PROJECT_DIR"


# ── 5. Create the real .env — NEVER commit this, it's gitignored ───────────
# Fastest path if you already have a working .env from local testing: copy it
# up securely FROM YOUR LOCAL MACHINE (run this line on your Windows machine,
# not on the server):
#   scp .env twilight@srv1569539:~/Twilight_Agent/.env
#
# Otherwise, start from the template on the server and fill in real values:
cp .env.example .env
nano .env
# Fill in for real: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, NVIDIA_API_KEY,
# OPENROUTER_API_KEY, GEMINI_API_KEY, GEMINI_API_KEY_FALLBACK (optional),
# WA_MONITORED_CHATS (comma-separated group-name substrings to watch).


# ── 6. Storage/log/auth folders ─────────────────────────────────────────────
# Created automatically on first run too, but fine to pre-create.
mkdir -p storage logs baileys_auth


# ── 7. Install PM2 — one process manager for BOTH the gateway and the agent ─
# Global install, shared across every project on this server — safe even if
# it's already installed for something else here.
npm install -g pm2


# ── 8. FIRST RUN ONLY — scan the WhatsApp QR code ───────────────────────────
# baileys_auth/ is empty on a fresh deploy, so PM2 can't run this headless
# yet — it needs a live QR scan first. Run the gateway in the FOREGROUND:
node src/index.js
# It prints an ASCII QR code (and a browser URL) in this terminal. Scan it
# from your phone: WhatsApp -> Settings -> Linked Devices -> Link a Device.
# Once you see "✅ WhatsApp connected and listening for messages", press
# Ctrl+C to stop this foreground run. baileys_auth/ now holds a persisted
# session, so PM2 can start it headless from here on (step 9).


# ── 9. Start both processes under PM2 ───────────────────────────────────────
# Process names used here: "gateway" and "agent" (matches what's actually
# running in production as of 2026-07-30 — see the note below).
#
# IMPORTANT: check `pm2 list` FIRST. If a process by either of these names
# (or any other name running this same app) is already up, do NOT start a
# second copy — two Node gateways sharing one baileys_auth/ session fight
# each other over the WhatsApp connection (440 "Stream Errored (conflict)"),
# and two Python agents can't both bind 127.0.0.1:8000 (the second errors
# with "address already in use" — this exact pair of bugs happened once
# already from re-running this step against an already-running deploy). If
# one already exists, use `pm2 restart <name>` instead of `pm2 start` again.
pm2 list

pm2 start src/index.js --name gateway

# Point PM2 at the venv's OWN python, not system python, so it uses the deps
# installed in step 4 rather than whatever's global (or another project's).
pm2 start "$PROJECT_DIR/agent/main.py" \
  --name agent \
  --interpreter "$PROJECT_DIR/agent/venv/bin/python3" \
  --cwd "$PROJECT_DIR/agent"

# ── 10. Persist across server reboots ───────────────────────────────────────
pm2 save
pm2 startup
# ^ this PRINTS a command starting with `sudo env PATH=...` — copy that
# printed line and run it once (it registers pm2's boot script with systemd).


# ── 11. Verify everything is actually up ────────────────────────────────────
pm2 status
pm2 logs gateway --lines 50
pm2 logs agent --lines 50
curl http://localhost:8000/health
# Expect: {"status":"ok","tools":["extract_petty_cash","set_opening_balance","extract_maintenance_bill"]}


# ============================================================================
# DAY-TO-DAY OPERATIONS (reference only — not part of first deploy)
# ============================================================================

# View live logs:
#   pm2 logs gateway
#   pm2 logs agent

# Redeploy after a code change:
#   cd ~/Twilight_Agent && git pull
#   npm install --omit=dev                                        # only if package.json changed
#   source agent/venv/bin/activate && pip install -r agent/requirements.txt && deactivate   # only if requirements.txt changed
#   pm2 restart gateway
#   pm2 restart agent

# Stop everything for this project (won't touch other projects' pm2 processes):
#   pm2 stop gateway agent

# Re-link WhatsApp (e.g. after "Bad MAC" session errors, or if the phone
# unlinked the device):
#   pm2 stop gateway
#   rm -rf baileys_auth
#   node src/index.js        # foreground, scan the fresh QR (see step 8)
#   # Ctrl+C once connected, then:
#   pm2 restart gateway

# Check disk usage (storage/logs auto-prune, but worth an occasional glance):
#   du -sh storage logs
# ============================================================================
