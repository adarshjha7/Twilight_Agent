const { createClient } = require('@supabase/supabase-js');

// // // ===== PRODUCTION DB (fpmfqdhyfclwtpqhhhtp) — COMMENTED OUT =====
// Stage-2 Branch credentials (Main)
// const SUPABASE_URL = process.env.SUPABASE_URL || "https://aeiqpqurvdrejsunhecp.supabase.co";
// const SUPABASE_PUBLISHABLE_KEY = process.env.SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFlaXFwcXVydmRyZWpzdW5oZWNwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY3NTM4MjcsImV4cCI6MjA5MjMyOTgyN30.U5I0c_DNa8es108BxK3b2Im9ODo0t4kYeSWGVwEm_gI";
o
// Stage-2 Branch credentials (Stage) - ACTIVE
const SUPABASE_URL = process.env.SUPABASE_URL || "https://aeiqpqurvdrejsunhecp.supabase.co";
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFlaXFwcXVydmRyZWpzdW5oZWNwIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3Njc1MzgyNywiZXhwIjoyMDkyMzI5ODI3fQ.s7gciO0Pe34J3j0CB0HRUSMckOf1odIqguhIVY6AYXs";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

module.exports = { supabase };
