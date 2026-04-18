-- Migration number: 0005    Execution Log for AI training data

CREATE TABLE IF NOT EXISTS execution_log (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  -- Identifikation
  execution_id                TEXT    NOT NULL,
  chunk_index                 INTEGER NOT NULL,
  action                      TEXT    NOT NULL,
  timestamp_ms                REAL    NOT NULL,
  user_id                     TEXT    NOT NULL DEFAULT '',
  bot_id                      TEXT    NOT NULL DEFAULT '',
  pair                        TEXT    NOT NULL,
  exchange_maker              TEXT    NOT NULL,
  exchange_taker              TEXT    NOT NULL,
  instrument_maker            TEXT    NOT NULL,
  instrument_taker            TEXT    NOT NULL,
  maker_side                  TEXT    NOT NULL,
  -- Orderbook-Snapshot zum Entscheidungszeitpunkt
  snapshot_mid_maker          REAL,
  snapshot_mid_taker          REAL,
  snapshot_best_bid_maker     REAL,
  snapshot_best_ask_maker     REAL,
  snapshot_best_bid_taker     REAL,
  snapshot_best_ask_taker     REAL,
  snapshot_spread_bps         REAL,
  snapshot_bid_ask_spread_maker_bps REAL,
  snapshot_bid_ask_spread_taker_bps REAL,
  snapshot_ohi_maker          REAL,
  snapshot_ohi_taker          REAL,
  snapshot_depth_5bps_maker   REAL,
  snapshot_depth_5bps_taker   REAL,
  snapshot_depth_20bps_maker  REAL,
  snapshot_depth_20bps_taker  REAL,
  -- Execution-Ergebnis
  target_qty                  REAL,
  filled_qty_maker            REAL,
  filled_qty_taker            REAL,
  fill_price_maker            REAL,
  fill_price_taker            REAL,
  realized_slippage_maker_bps REAL,
  realized_slippage_taker_bps REAL,
  chase_rounds                INTEGER,
  chunk_duration_s            REAL,
  success                     INTEGER NOT NULL DEFAULT 1,
  error                       TEXT,
  -- Markt-Kontext
  funding_rate_long           REAL,
  funding_rate_short          REAL,
  funding_spread              REAL,
  v4_spread_consistency       REAL,
  v4_confidence_score         REAL,
  hour_of_day                 INTEGER,
  day_of_week                 INTEGER,
  btc_volatility_1h           REAL,
  -- Config-Kontext
  use_depth_spread            INTEGER,
  taker_drift_guard           INTEGER,
  max_slippage_bps_cfg        REAL,
  maker_timeout_ms            INTEGER,
  reduce_only                 INTEGER NOT NULL DEFAULT 0,
  simulation                  INTEGER NOT NULL DEFAULT 0,
  UNIQUE(execution_id, chunk_index, action)
);
CREATE INDEX IF NOT EXISTS idx_el_user_ts ON execution_log(user_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_el_bot_ts ON execution_log(bot_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_el_exec ON execution_log(execution_id);
CREATE INDEX IF NOT EXISTS idx_el_pair ON execution_log(pair, timestamp_ms);
