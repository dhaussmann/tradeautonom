/**
 * Tradeability evaluation — pure function over a BookSnapshot.
 *
 * Motivation: some symbols are formally listed by an exchange (and therefore
 * appear in `/symbols` / `/markets`) but are not actually tradeable in
 * practice — typical failure mode is a one-sided book (only bids, no asks,
 * or vice versa) or a stale single-quote artefact.
 *
 * Concrete trigger: Nado's `ARB-PERP` (product_id 62) currently advertises
 * only bids and no asks; the book is one-sided which means a long-position
 * cannot be entered with a market/IOC order, and any bot configured to use
 * Nado for ARB will fail to trade. The same can happen any time a market is
 * paused, delisted, or experiencing a liquidity outage.
 *
 * This module returns a hard pass/fail and a machine-readable reason. It is
 * called periodically (~hourly) by AggregatorDO over every (exchange,symbol)
 * pair we track and the result is annotated onto `/tracked` and
 * `/discovery/pairs` so the frontend can hide non-tradeable legs.
 */
import type { BookSnapshot } from "../types";

export interface TradeabilityOptions {
  /** Max allowed top-of-book spread in basis points before we flag the
   *  market as untradeable. 500 bps = 5 % — meant to catch stale single
   *  quotes (e.g. ARB-PERP on Nado at 0.128 vs. fair ~0.124 ⇒ tens of bps,
   *  fine; but a 100 % outlier vs. fair would trip this). */
  maxTopOfBookSpreadBps: number;
  /** Max age of the snapshot in ms. Older snapshots count as untradeable. */
  maxAgeMs: number;
  /** Reference time used for staleness calculation (defaults to now). */
  now?: number;
}

export const DEFAULT_TRADEABILITY_OPTIONS: TradeabilityOptions = {
  maxTopOfBookSpreadBps: 500, // 5 %
  maxAgeMs: 5 * 60 * 1000,    // 5 min
};

export type TradeabilityReason =
  | "no_book"
  | "no_bids"
  | "no_asks"
  | "disconnected"
  | "stale"
  | "crossed_book"
  | "spread_too_wide"
  | "invalid_price";

export interface TradeabilityResult {
  tradeable: boolean;
  reason: TradeabilityReason | null;
  /** Top-of-book bid×ask spread in bps (null if either side missing). */
  spread_bps: number | null;
  /** Best bid / best ask at evaluation time (null if missing). */
  best_bid: number | null;
  best_ask: number | null;
  /** Snapshot age in ms (null if no snapshot). */
  age_ms: number | null;
  /** When the evaluation ran (epoch ms). */
  checked_at: number;
}

export function evaluateTradeability(
  snap: BookSnapshot | null | undefined,
  opts: Partial<TradeabilityOptions> = {},
): TradeabilityResult {
  const o: TradeabilityOptions = { ...DEFAULT_TRADEABILITY_OPTIONS, ...opts };
  const now = o.now ?? Date.now();

  if (!snap) {
    return {
      tradeable: false,
      reason: "no_book",
      spread_bps: null,
      best_bid: null,
      best_ask: null,
      age_ms: null,
      checked_at: now,
    };
  }

  const ageMs = snap.timestamp_ms ? now - snap.timestamp_ms : null;
  const bestBid = snap.bids.length > 0 ? snap.bids[0]![0] : null;
  const bestAsk = snap.asks.length > 0 ? snap.asks[0]![0] : null;
  const spreadBps =
    bestBid !== null && bestAsk !== null && bestBid > 0
      ? ((bestAsk - bestBid) / bestBid) * 10_000
      : null;

  if (!snap.connected) {
    return {
      tradeable: false,
      reason: "disconnected",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  if (ageMs !== null && ageMs > o.maxAgeMs) {
    return {
      tradeable: false,
      reason: "stale",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  if (snap.bids.length === 0 && snap.asks.length === 0) {
    return {
      tradeable: false,
      reason: "no_book",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }
  if (snap.bids.length === 0) {
    return {
      tradeable: false,
      reason: "no_bids",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }
  if (snap.asks.length === 0) {
    return {
      tradeable: false,
      reason: "no_asks",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  if (
    bestBid === null ||
    bestAsk === null ||
    !Number.isFinite(bestBid) ||
    !Number.isFinite(bestAsk) ||
    bestBid <= 0 ||
    bestAsk <= 0
  ) {
    return {
      tradeable: false,
      reason: "invalid_price",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  if (bestAsk < bestBid) {
    return {
      tradeable: false,
      reason: "crossed_book",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  if (spreadBps !== null && spreadBps > o.maxTopOfBookSpreadBps) {
    return {
      tradeable: false,
      reason: "spread_too_wide",
      spread_bps: spreadBps,
      best_bid: bestBid,
      best_ask: bestAsk,
      age_ms: ageMs,
      checked_at: now,
    };
  }

  return {
    tradeable: true,
    reason: null,
    spread_bps: spreadBps,
    best_bid: bestBid,
    best_ask: bestAsk,
    age_ms: ageMs,
    checked_at: now,
  };
}

/** Storage shape for the rolling tradeability map. */
export type TradeabilityMap = Record<string, TradeabilityResult>;

export function tradeabilityKey(exchange: string, symbol: string): string {
  return `${exchange}:${symbol}`;
}
