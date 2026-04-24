/**
 * Cross-exchange arbitrage math.
 *
 * 1:1 port of deploy/monitor/monitor_service.py:
 *   - _estimate_fill_price (line 439-452)
 *   - _binary_search_arb_qty (line 455-499)
 *   - _min_profit_bps       (line 163-171)
 *   - _find_arb_for_token   (line 502-593)
 *
 * All fee calculations intentionally match V1 byte-for-byte so that DNA-bot
 * (app/dna_bot.py) can switch between V1 Photon OMS and V2 oms-v2 without
 * observing numerical drift beyond small float-ordering differences.
 */

import type {
  ArbOpportunity,
  BookSnapshot,
  MarketMeta,
} from "../types";

// ── V1 defaults (keep in sync with monitor_service.py) ───────────

/** Taker fee percentage (percent, not bps). */
export const TAKER_FEE_PCT: Record<string, number> = {
  extended: 0.0225,
  nado: 0.035,
  grvt: 0.039,
  variational: 0.04, // default for unknown exchanges in V1
};

export const ARB_FEE_BUFFER_BPS = 1.0;
export const ARB_MAX_NOTIONAL_USD = 50_000;

/**
 * Default arb-eligible exchange set. Matches V1 Photon default
 * (`OMS_ARB_EXCHANGES=grvt,extended,nado`). Variational excluded because
 * its books are synthetic USD-notional tiers rather than real depth.
 */
export const ARB_EXCHANGES: ReadonlySet<string> = new Set([
  "extended",
  "grvt",
  "nado",
]);

/**
 * Tokens always excluded from arb (equity / FX perpetuals where cross-DEX
 * arb is meaningless or the book is dominated by one venue).
 */
export const ARB_EXCLUDED_TOKENS: ReadonlySet<string> = new Set([
  "WTI",
  "MEGA",
  "AMZN",
  "AAPL",
  "TSLA",
  "HOOD",
  "META",
  "USDJPY",
]);

/** Advisory scan interval (event-driven recomputation, not used as a loop). */
export const ARB_SCAN_INTERVAL_S = 0.2;

/** Binary search min quantity (matches Python default). */
const BINARY_MIN_QTY = 0.001;
const BINARY_ITERATIONS = 12;

// ── Pure helpers ──────────────────────────────────────────────────

/**
 * Walk orderbook levels and return VWAP fill price for `qty`.
 * Matches monitor_service.py::_estimate_fill_price.
 */
export function estimateFillPrice(
  levels: ReadonlyArray<[number, number]>,
  qty: number,
): number {
  let remaining = qty;
  let totalCost = 0.0;
  for (const [price, size] of levels) {
    const fill = Math.min(remaining, size);
    totalCost += fill * price;
    remaining -= fill;
    if (remaining <= 0) break;
  }
  const filled = qty - remaining;
  return filled > 0 ? totalCost / filled : 0.0;
}

/**
 * Find the maximum quantity where cross-venue arb profit > min_profit_bps.
 * Returns [max_qty, buy_fill_vwap, sell_fill_vwap].
 * Matches monitor_service.py::_binary_search_arb_qty.
 */
export function binarySearchArbQty(
  buyAsks: ReadonlyArray<[number, number]>,
  sellBids: ReadonlyArray<[number, number]>,
  midPrice: number,
  upperNotional: number,
  minProfitBps: number,
): [number, number, number] {
  if (midPrice <= 0) return [0, 0, 0];

  let hi = upperNotional / midPrice;
  const lo0 = BINARY_MIN_QTY;
  if (hi <= lo0) return [0, 0, 0];

  let lo = lo0;
  let bestQty = 0.0;
  let bestBuy = 0.0;
  let bestSell = 0.0;

  for (let i = 0; i < BINARY_ITERATIONS; i++) {
    const mid = (lo + hi) / 2.0;
    const buyFill = estimateFillPrice(buyAsks, mid);
    const sellFill = estimateFillPrice(sellBids, mid);

    if (buyFill <= 0 || sellFill <= 0) {
      hi = mid;
      continue;
    }
    const profitBps = ((sellFill - buyFill) / buyFill) * 10000;
    if (profitBps >= minProfitBps) {
      bestQty = mid;
      bestBuy = buyFill;
      bestSell = sellFill;
      lo = mid; // can go bigger
    } else {
      hi = mid; // too much slippage, go smaller
    }
  }
  return [bestQty, bestBuy, bestSell];
}

/**
 * Full-fee minimum profit threshold in bps for a buy-exchange + sell-exchange pair.
 * Matches monitor_service.py::_min_profit_bps.
 *
 *   fee_pct_sum * 2 * 100 + buffer
 *
 * (The *2 accounts for round-trip close-out; V1 convention.)
 */
export function minProfitBps(buyExch: string, sellExch: string): number {
  const buyFee = TAKER_FEE_PCT[buyExch] ?? 0.04;
  const sellFee = TAKER_FEE_PCT[sellExch] ?? 0.04;
  return (buyFee + sellFee) * 2 * 100 + ARB_FEE_BUFFER_BPS;
}

// ── Core scan ─────────────────────────────────────────────────────

/**
 * Meta-lookup helper for a single (exchange, token) pair.
 */
function lookupMeta(
  meta: Record<string, Record<string, MarketMeta>>,
  exch: string,
  token: string,
): MarketMeta {
  return (
    meta?.[exch]?.[token] ?? {
      maxLeverage: 1,
      minOrderSize: 0,
      qtyStep: 0,
    }
  );
}

export interface FindArbConfig {
  /** exchange → token → meta. */
  meta: Record<string, Record<string, MarketMeta>>;
  /** Optional override of the minimum profit threshold (bps). */
  overrideMinBps?: number;
  /** Which exchanges are eligible (defaults to ARB_EXCHANGES). */
  eligibleExchanges?: ReadonlySet<string>;
  /** Upper bound on notional for sizing (defaults to ARB_MAX_NOTIONAL_USD). */
  maxNotionalUsd?: number;
}

/**
 * Check all exchange pairs for a token and return actionable arb opportunities.
 * Matches monitor_service.py::_find_arb_for_token.
 *
 * @param token         Base token (e.g. "BTC").
 * @param exchangeMap   exchange → symbol for this token (from discovery).
 * @param books         Keyed by "exchange:symbol" → current BookSnapshot.
 * @param config        Meta lookups + optional overrides.
 */
export function findArbForToken(
  token: string,
  exchangeMap: Record<string, string>,
  books: Map<string, BookSnapshot>,
  config: FindArbConfig,
): ArbOpportunity[] {
  const nowMs = Date.now();
  const opps: ArbOpportunity[] = [];
  const eligible = config.eligibleExchanges ?? ARB_EXCHANGES;
  const upperNotional = config.maxNotionalUsd ?? ARB_MAX_NOTIONAL_USD;

  type Leg = { exch: string; sym: string; snap: BookSnapshot };
  const legs: Leg[] = [];
  for (const [exch, sym] of Object.entries(exchangeMap)) {
    if (!eligible.has(exch)) continue;
    const snap = books.get(`${exch}:${sym}`);
    if (!snap) continue;
    if (!snap.connected) continue;
    if (snap.bids.length === 0 || snap.asks.length === 0) continue;
    legs.push({ exch, sym, snap });
  }
  if (legs.length < 2) return opps;

  // Compare all directional pairs
  for (let i = 0; i < legs.length; i++) {
    for (let j = i + 1; j < legs.length; j++) {
      const a = legs[i]!;
      const b = legs[j]!;

      const bestBidA = a.snap.bids[0]![0];
      const bestAskA = a.snap.asks[0]![0];
      const bestBidB = b.snap.bids[0]![0];
      const bestAskB = b.snap.asks[0]![0];

      // Both directions: buy-A/sell-B, buy-B/sell-A
      const dirs: Array<{
        buy: Leg; sell: Leg; buyAsk: number; sellBid: number;
      }> = [
        { buy: a, sell: b, buyAsk: bestAskA, sellBid: bestBidB },
        { buy: b, sell: a, buyAsk: bestAskB, sellBid: bestBidA },
      ];

      for (const d of dirs) {
        if (d.sellBid <= d.buyAsk) continue;

        const bboSpreadBps = ((d.sellBid - d.buyAsk) / d.buyAsk) * 10000;
        const midPrice = (d.buyAsk + d.sellBid) / 2.0;

        const fullFeeBps = minProfitBps(d.buy.exch, d.sell.exch);
        const pairMinBps =
          config.overrideMinBps !== undefined
            ? config.overrideMinBps
            : fullFeeBps;

        const [maxQty, buyVwap, sellVwap] = binarySearchArbQty(
          d.buy.snap.asks,
          d.sell.snap.bids,
          midPrice,
          upperNotional,
          pairMinBps,
        );
        if (maxQty <= 0) continue;

        const netProfitBps = ((sellVwap - buyVwap) / buyVwap) * 10000;
        const maxNotional = maxQty * midPrice;

        const buyMeta = lookupMeta(config.meta, d.buy.exch, token);
        const sellMeta = lookupMeta(config.meta, d.sell.exch, token);

        opps.push({
          token,
          buy_exchange: d.buy.exch,
          buy_symbol: d.buy.sym,
          sell_exchange: d.sell.exch,
          sell_symbol: d.sell.sym,
          buy_price_bbo: d.buyAsk,
          sell_price_bbo: d.sellBid,
          bbo_spread_bps: round2(bboSpreadBps),
          buy_fill_vwap: round6(buyVwap),
          sell_fill_vwap: round6(sellVwap),
          net_profit_bps: round2(netProfitBps),
          fee_threshold_bps: round1(fullFeeBps),
          max_qty: round6(maxQty),
          max_notional_usd: round2(maxNotional),
          timestamp_ms: nowMs,
          buy_max_leverage: buyMeta.maxLeverage,
          sell_max_leverage: sellMeta.maxLeverage,
          buy_min_order_size: buyMeta.minOrderSize,
          sell_min_order_size: sellMeta.minOrderSize,
          buy_qty_step: buyMeta.qtyStep,
          sell_qty_step: sellMeta.qtyStep,
        });
      }
    }
  }
  return opps;
}

// ── Rounding helpers (match Python round(x, n) half-even semantics closely) ─

function round1(v: number): number { return Math.round(v * 10) / 10; }
function round2(v: number): number { return Math.round(v * 100) / 100; }
function round6(v: number): number { return Math.round(v * 1_000_000) / 1_000_000; }
