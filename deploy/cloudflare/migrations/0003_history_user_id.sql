-- Migration number: 0003 	 Add user_id to history tables for multi-user isolation

ALTER TABLE equity_snapshots ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE position_snapshots ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE trades ADD COLUMN user_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_equity_user_ts ON equity_snapshots(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_pos_user_ts ON position_snapshots(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_user_closed ON trades(user_id, closed_at);
