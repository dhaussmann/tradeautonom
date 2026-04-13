-- Migration number: 0001 	 2026-04-05T13:39:10.258Z

-- Equity curve per exchange (one row per exchange per snapshot interval)
CREATE TABLE IF NOT EXISTS equity_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  exchange   TEXT    NOT NULL,
  equity     REAL    NOT NULL DEFAULT 0,
  unrealized_pnl REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_equity_exchange_ts ON equity_snapshots(exchange, ts);

-- Periodic position snapshots (one row per position per snapshot interval)
CREATE TABLE IF NOT EXISTS position_snapshots (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                 INTEGER NOT NULL,
  exchange           TEXT    NOT NULL,
  token              TEXT    NOT NULL,
  instrument         TEXT    NOT NULL,
  side               TEXT    NOT NULL,
  size               REAL    NOT NULL DEFAULT 0,
  entry_price        REAL    NOT NULL DEFAULT 0,
  mark_price         REAL    NOT NULL DEFAULT 0,
  unrealized_pnl     REAL    NOT NULL DEFAULT 0,
  realized_pnl       REAL    NOT NULL DEFAULT 0,
  cumulative_funding REAL    NOT NULL DEFAULT 0,
  funding_rate       REAL    NOT NULL DEFAULT 0,
  leverage           REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pos_ts ON position_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_pos_token_ts ON position_snapshots(token, ts);
CREATE INDEX IF NOT EXISTS idx_pos_exchange_ts ON position_snapshots(exchange, ts);

-- Completed trades (position open -> close)
CREATE TABLE IF NOT EXISTS trades (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange           TEXT    NOT NULL,
  token              TEXT    NOT NULL,
  instrument         TEXT    NOT NULL,
  side               TEXT    NOT NULL,
  size               REAL    NOT NULL DEFAULT 0,
  entry_price        REAL    NOT NULL DEFAULT 0,
  exit_price         REAL    NOT NULL DEFAULT 0,
  opened_at          INTEGER NOT NULL DEFAULT 0,
  closed_at          INTEGER NOT NULL DEFAULT 0,
  realized_pnl       REAL    NOT NULL DEFAULT 0,
  cumulative_funding REAL    NOT NULL DEFAULT 0,
  total_pnl          REAL    NOT NULL DEFAULT 0,
  pair_token         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token, closed_at);
