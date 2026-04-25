-- Phase F.4 M7: audit log for V1↔V2 backend migrations.
--
-- Each migration attempt (whether success or failure) writes one row.
-- Used by the admin UI to show recent migration history and diagnose
-- failures.

CREATE TABLE IF NOT EXISTS migration_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    direction TEXT NOT NULL,             -- 'to_cf' | 'to_photon'
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                -- 'in_progress' | 'success' | 'failed'
    error TEXT,
    tar_bytes INTEGER,
    forced INTEGER NOT NULL DEFAULT 0,
    trace TEXT                           -- JSON array of step messages
);

CREATE INDEX IF NOT EXISTS idx_migration_audit_user ON migration_audit(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_migration_audit_status ON migration_audit(status, started_at DESC);
