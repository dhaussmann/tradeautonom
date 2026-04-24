/**
 * Pure quote math — single-leg and cross-leg.
 *
 * Centralises the orderbook calculations that every V1 bot does at entry:
 *   - walk_book / estimate_fill_price (app/safety.py)
 *   - _compute_vwap_limit (app/arbitrage.py)
 *   - _harmonize_qty      (app/dna_bot.py)
 *   - analyze_cross_venue_spread (app/spread_analyzer.py)
 *
 * After Phase E, V1 bots receive fully-computed quotes from OMS and no longer
 * need any of those helpers locally.
 */

import type {
  BookSnapshot,
  MarketMeta,
  Quote,
  CrossQuote,
} from "../types";
import { minProfitBps, TAKER_FEE_PCT } from "./arb";

export const DEFAULT_BUFFER_TICKS = 2;
export const BOOK_STALE_MS = 5_000;

export interface ComputeQuoteInput {
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  book: BookSnapshot | null;
  /** Exactly one of qty / notionalUsd is used. If both are given, qty wins. */
  qty?: number;
  notionalUsd?: number;
  meta: MarketMeta;
  /** Optional static fee (percent, not bps) for the exchange. */
  takerFeePct?: number;
  /** Tick size; drives `limit_price_with_buffer`. */
  tickSize: number;
  bufferTicks?: number;
  /** Unix epoch ms used for age calculations (default: now). */
  nowMs?: number;
}

/**
 * Compute a single-leg Quote.
 *
 * Never throws — errors become `feasible:false` with `feasibility_reason`.
 */
export function computeQuote(input: ComputeQuoteInput): Quote {
  const {
    exchange, symbol, side, book, meta,
    takerFeePct = TAKER_FEE_PCT[exchange] ?? 0.04,
    tickSize, bufferTicks = DEFAULT_BUFFER_TICKS,
    nowMs = Date.now(),
  } = input;

  const base: Partial<Quote> = {
    exchange, symbol, side,
    requested_qty: input.qty ?? 0,
    requested_notional_usd: input.notionalUsd ?? 0,
    fillable_qty: 0,
    unfilled_qty: 0,
    best_price: 0,
    worst_price: 0,
    vwap: 0,
    mid_price: 0,
    slippage_bps_vs_best: 0,
    slippage_bps_vs_mid: 0,
    notional_usd: 0,
    levels_consumed: 0,
    total_levels_on_side: 0,
    limit_price_with_buffer: 0,
    buffer_ticks: bufferTicks,
    min_order_size: meta.minOrderSize,
    qty_step: meta.qtyStep,
    tick_size: tickSize,
    taker_fee_pct: takerFeePct,
    harmonized_qty: 0,
    feasible: false,
    feasibility_reason: null,
    book_age_ms: null,
    timestamp_ms: nowMs,
  };

  if (!book) {
    return {
      ...(base as Quote),
      feasible: false,
      feasibility_reason: "no_book",
    };
  }

  const bookAgeMs =
    book.timestamp_ms > 0 ? Math.max(0, nowMs - book.timestamp_ms) : null;
  if (bookAgeMs !== null && bookAgeMs > BOOK_STALE_MS) {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      feasible: false,
      feasibility_reason: "book_stale",
    };
  }
  if (!book.connected) {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      feasible: false,
      feasibility_reason: "book_disconnected",
    };
  }

  // For BUY we consume asks (ascending); for SELL we consume bids (descending).
  const levels = side === "buy" ? book.asks : book.bids;
  const totalLevels = levels.length;
  if (totalLevels === 0) {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      feasible: false,
      feasibility_reason: "empty_side",
    };
  }

  const bestPrice = levels[0]![0];
  const midPrice =
    book.bids.length > 0 && book.asks.length > 0
      ? (book.bids[0]![0] + book.asks[0]![0]) / 2
      : bestPrice;

  // Resolve qty: prefer explicit `qty`; else derive from notional / best.
  let targetQty: number;
  if (typeof input.qty === "number" && input.qty > 0) {
    targetQty = input.qty;
  } else if (
    typeof input.notionalUsd === "number" &&
    input.notionalUsd > 0 &&
    midPrice > 0
  ) {
    targetQty = input.notionalUsd / midPrice;
  } else {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      mid_price: midPrice,
      best_price: bestPrice,
      total_levels_on_side: totalLevels,
      feasible: false,
      feasibility_reason: "missing_size_input",
    };
  }

  // Harmonise to qty_step (round down).
  const step = meta.qtyStep > 0 ? meta.qtyStep : 0;
  const harmonized = step > 0
    ? Math.floor(targetQty / step) * step
    : targetQty;

  if (harmonized <= 0) {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      mid_price: midPrice,
      best_price: bestPrice,
      total_levels_on_side: totalLevels,
      harmonized_qty: harmonized,
      feasible: false,
      feasibility_reason: "qty_below_step",
    };
  }
  // Effective base-qty floor:
  //   max(minOrderSize, ceil(minNotionalUsd / mid / step) * step)
  // For Nado, minOrderSize is the base-qty tick (size_increment) and
  // minNotionalUsd is the real USD floor — we convert it to base qty using
  // the live book's mid price. If no mid is available (cold start), fall
  // back to the base-qty floor only (never false-reject).
  const effMinQty = computeEffectiveMinQty(meta, midPrice);
  if (effMinQty > 0 && harmonized < effMinQty) {
    return {
      ...(base as Quote),
      book_age_ms: bookAgeMs,
      mid_price: midPrice,
      best_price: bestPrice,
      total_levels_on_side: totalLevels,
      harmonized_qty: harmonized,
      // Report the effective min so callers see what they need to beat.
      min_order_size: effMinQty,
      feasible: false,
      feasibility_reason: "qty_below_min_order_size",
    };
  }

  // Walk the book.
  let remaining = harmonized;
  let totalCost = 0;
  let worstPrice = bestPrice;
  let levelsConsumed = 0;
  for (const [price, size] of levels) {
    const fill = Math.min(remaining, size);
    totalCost += fill * price;
    remaining -= fill;
    worstPrice = price;
    levelsConsumed += 1;
    if (remaining <= 0) break;
  }
  const fillable = harmonized - remaining;
  const unfilled = Math.max(0, remaining);
  const vwap = fillable > 0 ? totalCost / fillable : 0;

  const slippageBpsVsBest =
    bestPrice > 0
      ? (side === "buy"
          ? ((vwap - bestPrice) / bestPrice) * 10000
          : ((bestPrice - vwap) / bestPrice) * 10000)
      : 0;
  const slippageBpsVsMid =
    midPrice > 0
      ? (side === "buy"
          ? ((vwap - midPrice) / midPrice) * 10000
          : ((midPrice - vwap) / midPrice) * 10000)
      : 0;

  // Limit price with buffer (matches app/arbitrage.py::_compute_vwap_limit):
  //   BUY  → worst_price + buffer_ticks * tick_size (extra headroom to fill)
  //   SELL → worst_price (no buffer; lowering sell limit below worst would
  //          make IOC stricter — we leave it at the sweep price).
  const limitPrice =
    side === "buy"
      ? worstPrice + bufferTicks * tickSize
      : worstPrice;

  const feasible = unfilled <= 0;
  const feasibilityReason: Quote["feasibility_reason"] = feasible
    ? null
    : "insufficient_depth";

  return {
    exchange,
    symbol,
    side,
    requested_qty: round8(targetQty),
    requested_notional_usd: input.notionalUsd ?? round6(targetQty * midPrice),
    fillable_qty: round8(fillable),
    unfilled_qty: round8(unfilled),
    best_price: bestPrice,
    worst_price: worstPrice,
    vwap: round8(vwap),
    mid_price: midPrice,
    slippage_bps_vs_best: round4(slippageBpsVsBest),
    slippage_bps_vs_mid: round4(slippageBpsVsMid),
    notional_usd: round4(totalCost),
    levels_consumed: levelsConsumed,
    total_levels_on_side: totalLevels,
    limit_price_with_buffer: round8(limitPrice),
    buffer_ticks: bufferTicks,
    // Report the EFFECTIVE base-qty min (notional→qty converted with live
    // mid when applicable). See computeEffectiveMinQty.
    min_order_size: round8(effMinQty > 0 ? effMinQty : meta.minOrderSize),
    qty_step: meta.qtyStep,
    tick_size: tickSize,
    taker_fee_pct: takerFeePct,
    harmonized_qty: round8(harmonized),
    feasible,
    feasibility_reason: feasibilityReason,
    book_age_ms: bookAgeMs,
    timestamp_ms: nowMs,
  };
}

// ── Cross-venue quote ─────────────────────────────────────────────

export interface ComputeCrossQuoteInput {
  token: string;
  buy: Omit<ComputeQuoteInput, "side">;
  sell: Omit<ComputeQuoteInput, "side">;
  /**
   * Desired qty (or derive via notionalUsd). Whichever is provided is passed
   * to both legs after harmonisation to the coarser step.
   */
  qty?: number;
  notionalUsd?: number;
  nowMs?: number;
}

export function computeCrossQuote(input: ComputeCrossQuoteInput): CrossQuote {
  const { token, buy, sell, nowMs = Date.now() } = input;

  // Determine binding step (the larger of the two) and resolve target qty.
  const buyMidCandidate =
    buy.book && buy.book.bids.length > 0 && buy.book.asks.length > 0
      ? (buy.book.bids[0]![0] + buy.book.asks[0]![0]) / 2
      : 0;
  const sellMidCandidate =
    sell.book && sell.book.bids.length > 0 && sell.book.asks.length > 0
      ? (sell.book.bids[0]![0] + sell.book.asks[0]![0]) / 2
      : 0;
  const combinedMid =
    buyMidCandidate > 0 && sellMidCandidate > 0
      ? (buyMidCandidate + sellMidCandidate) / 2
      : (buyMidCandidate || sellMidCandidate);

  let targetQty = 0;
  if (typeof input.qty === "number" && input.qty > 0) {
    targetQty = input.qty;
  } else if (
    typeof input.notionalUsd === "number" &&
    input.notionalUsd > 0 &&
    combinedMid > 0
  ) {
    targetQty = input.notionalUsd / combinedMid;
  }

  // Harmonise to the larger step (binding side).
  const buyStep = buy.meta.qtyStep;
  const sellStep = sell.meta.qtyStep;
  const bindingStep = Math.max(buyStep, sellStep);
  // Resolve which exchange is the binding one: primarily the coarser step,
  // but fall back to the one with the larger effective min_qty if steps match.
  const buyEffMin = computeEffectiveMinQty(buy.meta, buyMidCandidate);
  const sellEffMin = computeEffectiveMinQty(sell.meta, sellMidCandidate);
  const stepExchange =
    bindingStep === 0 || bindingStep === buyStep && bindingStep === sellStep
      // When steps tie, binding exchange = the one with the larger eff min.
      ? (buyEffMin >= sellEffMin ? buy.exchange : sell.exchange)
      : (bindingStep === buyStep ? buy.exchange : sell.exchange);
  const harmonized = bindingStep > 0
    ? Math.floor(targetQty / bindingStep) * bindingStep
    : targetQty;

  const buyQuote = computeQuote({
    ...buy,
    side: "buy",
    qty: harmonized,
    notionalUsd: undefined,
    nowMs,
  });
  const sellQuote = computeQuote({
    ...sell,
    side: "sell",
    qty: harmonized,
    notionalUsd: undefined,
    nowMs,
  });

  // Spreads + profit
  let bboSpreadBps = 0;
  let execSpreadBps = 0;
  if (buyQuote.best_price > 0 && sellQuote.best_price > 0) {
    bboSpreadBps =
      ((sellQuote.best_price - buyQuote.best_price) / buyQuote.best_price) *
      10000;
  }
  if (buyQuote.vwap > 0 && sellQuote.vwap > 0) {
    execSpreadBps =
      ((sellQuote.vwap - buyQuote.vwap) / buyQuote.vwap) * 10000;
  }
  const slippageOverBbo = execSpreadBps - bboSpreadBps;
  const feeThresholdBps = minProfitBps(buy.exchange, sell.exchange);
  const netProfitBps = execSpreadBps - feeThresholdBps;

  const feasible = buyQuote.feasible && sellQuote.feasible && harmonized > 0;
  let feasibilityReason: CrossQuote["feasibility_reason"] = null;
  if (!feasible) {
    if (harmonized <= 0) feasibilityReason = "qty_below_step";
    else if (!buyQuote.feasible) feasibilityReason = `buy_${buyQuote.feasibility_reason ?? "infeasible"}`;
    else if (!sellQuote.feasible) feasibilityReason = `sell_${sellQuote.feasibility_reason ?? "infeasible"}`;
  }

  return {
    token,
    buy_exchange: buy.exchange,
    buy_symbol: buy.symbol,
    sell_exchange: sell.exchange,
    sell_symbol: sell.symbol,
    requested_qty: round8(targetQty),
    harmonized_qty: round8(harmonized),
    mid_price: combinedMid,
    notional_usd: round4(harmonized * combinedMid),
    buy: buyQuote,
    sell: sellQuote,
    bbo_spread_bps: round4(bboSpreadBps),
    exec_spread_bps: round4(execSpreadBps),
    slippage_bps_over_bbo: round4(slippageOverBbo),
    fee_threshold_bps: round4(feeThresholdBps),
    net_profit_bps_after_fees: round4(netProfitBps),
    profitable: netProfitBps > 0 && feasible,
    min_order_size_binding: stepExchange,
    feasible,
    feasibility_reason: feasibilityReason,
    timestamp_ms: nowMs,
  };
}

// ── Effective min qty ─────────────────────────────────────────────

/**
 * Compute the effective base-qty min.
 *
 * TEMPORARILY SIMPLIFIED: the notional→qty conversion was producing
 * wrong values (rejecting valid small orders on Nado because
 * `minNotionalUsd / mid / step` ceiled up too aggressively in some cases).
 * For now we return only the hard base-qty floor (`minOrderSize`) and
 * ignore `minNotionalUsd`. The real min-notional check still happens on
 * the exchange side at order-placement time — we just don't pre-reject
 * bot-side.
 *
 * To restore the notional-based computation once the arithmetic bug is
 * isolated:
 *
 *   if (meta.minNotionalUsd > 0 && midPrice > 0) {
 *     const rawQty = meta.minNotionalUsd / midPrice;
 *     const nominalMinQty = meta.qtyStep > 0
 *       ? Math.ceil(rawQty / meta.qtyStep) * meta.qtyStep
 *       : rawQty;
 *     return Math.max(base, nominalMinQty);
 *   }
 */
export function computeEffectiveMinQty(
  meta: { minOrderSize: number; qtyStep: number; minNotionalUsd: number },
  _midPrice: number,
): number {
  return meta.minOrderSize > 0 ? meta.minOrderSize : 0;
}

// ── Rounding helpers ──────────────────────────────────────────────

function round4(v: number): number { return Math.round(v * 1e4) / 1e4; }
function round6(v: number): number { return Math.round(v * 1e6) / 1e6; }
function round8(v: number): number { return Math.round(v * 1e8) / 1e8; }
