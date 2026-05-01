/**
 * Shared types for V2 OMS.
 */

import type { ExtendedOms } from "./exchanges/extended";
import type { GrvtOms } from "./exchanges/grvt";
import type { NadoOms } from "./exchanges/nado";
import type { VariationalOms } from "./exchanges/variational";
import type { RisexOms } from "./exchanges/risex";
import type { AggregatorDO } from "./aggregator";
import type { NadoRelayContainer } from "./nado-relay-container";
import type { ArbScannerDO } from "./arb-scanner";

/**
 * Cloudflare Analytics Engine binding interface.
 * See https://developers.cloudflare.com/analytics/analytics-engine/.
 */
export interface AnalyticsEngineDataset {
  writeDataPoint(event: {
    blobs?: string[];
    doubles?: number[];
    indexes?: string[];
  }): void;
}

export interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  GRVT_OMS: DurableObjectNamespace<GrvtOms>;
  NADO_OMS: DurableObjectNamespace<NadoOms>;
  VARIATIONAL_OMS: DurableObjectNamespace<VariationalOms>;
  /**
   * RisexOms — singleton DO holding the WebSocket to wss://ws.rise.trade/ws.
   * See src/exchanges/risex.ts.
   */
  RISEX_OMS: DurableObjectNamespace<RisexOms>;
  AGGREGATOR_DO: DurableObjectNamespace<AggregatorDO>;
  /**
   * Node.js container holding the permessage-deflate WebSocket to Nado.
   * See src/nado-relay-container.ts and container/nado-relay/.
   */
  NADO_RELAY: DurableObjectNamespace<NadoRelayContainer>;
  /**
   * Cross-exchange arbitrage scanner for DNA-bot support.
   * Serves /ws/arb and /arb/*. See src/arb-scanner.ts.
   */
  ARB_SCANNER: DurableObjectNamespace<ArbScannerDO>;
  /**
   * Analytics Engine dataset for historical PAXG/XAUT spread tracking.
   * Written by AggregatorDO via lib/gold-spread.ts whenever a Variational
   * book update affects either gold token.
   */
  GOLD_SPREAD: AnalyticsEngineDataset;
}

export interface BookSnapshot {
  exchange: string;
  symbol: string;
  bids: Array<[number, number]>; // sorted descending by price
  asks: Array<[number, number]>; // sorted ascending by price
  timestamp_ms: number;
  connected: boolean;
  updates: number;
  last_seq: number;
}

/** Bot-client WS protocol (V1-compatible Photon OMS wire format). */
export interface ClientSubscribe {
  action: "subscribe";
  exchange: string;
  symbol: string;
}

export interface ClientUnsubscribe {
  action: "unsubscribe";
  exchange: string;
  symbol: string;
}

/** Phase E: quote subscription over WS. */
export interface ClientQuote {
  action: "quote";
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  qty?: number;
  notional_usd?: number;
  buffer_ticks?: number;
}

export interface ClientUnquote {
  action: "unquote";
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  qty?: number;
  notional_usd?: number;
  buffer_ticks?: number;
}

export interface ClientQuoteCross {
  action: "quote_cross";
  token: string;
  buy_exchange: string;
  sell_exchange: string;
  qty?: number;
  notional_usd?: number;
  buffer_ticks?: number;
}

export interface ClientUnquoteCross {
  action: "unquote_cross";
  token: string;
  buy_exchange: string;
  sell_exchange: string;
  qty?: number;
  notional_usd?: number;
  buffer_ticks?: number;
}

export type ClientMessage =
  | ClientSubscribe
  | ClientUnsubscribe
  | ClientQuote
  | ClientUnquote
  | ClientQuoteCross
  | ClientUnquoteCross;

export interface ServerSubscribed {
  type: "subscribed";
  exchange: string;
  symbol: string;
  /** Phase E: static per-symbol meta (if discovered). */
  meta?: SymbolMeta | null;
}

/** Phase E: cheap per-push stats attached to every {type:"book"}. */
export interface BookPushStats {
  mid_price: number;
  bid_qty_cumsum: number[];
  ask_qty_cumsum: number[];
  bid_notional_cumsum: number[];
  ask_notional_cumsum: number[];
}

export interface ServerBookUpdate extends BookPushStats {
  type: "book";
  exchange: string;
  symbol: string;
  bids: Array<[number, number]>;
  asks: Array<[number, number]>;
  timestamp_ms: number;
}

export interface ServerError {
  error: string;
  detail?: string;
}

/**
 * Per-connection attachment persisted across hibernation. Phase E adds
 * quote subscription state (single-leg and cross).
 */
export interface WsAttachment {
  subs: string[];
  /** Encoded key: "exchange:symbol:side:qty_or_notional:mode:bufferTicks". */
  quoteSubs: QuoteSub[];
  /** Encoded key plus the token + two exchanges. */
  crossQuoteSubs: CrossQuoteSub[];
  connected_at: number;
}

export interface QuoteSub {
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  /** Exactly one is non-null. */
  qty: number | null;
  notional_usd: number | null;
  buffer_ticks: number;
  /** Last-send throttle timestamp (ms). Not persisted through hibernation. */
  last_sent_ms?: number;
}

export interface CrossQuoteSub {
  token: string;
  buy_exchange: string;
  sell_exchange: string;
  qty: number | null;
  notional_usd: number | null;
  buffer_ticks: number;
  last_sent_ms?: number;
}

/** Per-exchange market metadata collected during auto-discovery. */
export interface MarketMeta {
  maxLeverage: number;
  /**
   * Base-qty floor (e.g. 0.001 BTC). For Nado this is `size_increment`
   * because Nado's API `min_size` is actually USD notional (see
   * minNotionalUsd below).
   */
  minOrderSize: number;
  qtyStep: number;
  /**
   * USD-notional floor. Populated only for exchanges that publish one
   * (currently Nado). Quote + arb code converts this to an effective
   * base-qty threshold at evaluation time using the live book's mid price.
   * 0 means "no notional floor".
   */
  minNotionalUsd: number;
}

/** Phase E: Public /meta surface per exchange+symbol. */
export interface SymbolMeta {
  exchange: string;
  symbol: string;
  base_token: string;
  tick_size: number;
  /** Base-qty floor. For Nado this equals size_increment (see min_notional_usd). */
  min_order_size: number;
  /**
   * USD-notional floor, or null if the exchange does not publish one.
   * Nado publishes 100 USD on all perps; Extended/GRVT/Variational don't.
   */
  min_notional_usd: number | null;
  qty_step: number;
  max_leverage: number;
  taker_fee_pct: number;
  maker_fee_pct: number | null;
  funding_interval_s: number | null;
}

/** Auto-discovery result: base token → { exchange → symbol }. */
export type DiscoveredPairs = Record<string, Record<string, string>>;

// ── Arb scanner types (V1-wire-compatible) ────────────────────────

/**
 * Port of deploy/monitor/monitor_service.py::ArbOpportunity.
 *
 * Fields match 1:1 with Python `_arb_opp_to_dict`. DNA-bot (app/dna_bot.py)
 * consumes these fields by name; do not rename without coordinating.
 */
export interface ArbOpportunity {
  token: string;
  buy_exchange: string;
  buy_symbol: string;
  sell_exchange: string;
  sell_symbol: string;
  buy_price_bbo: number;
  sell_price_bbo: number;
  bbo_spread_bps: number;
  buy_fill_vwap: number;
  sell_fill_vwap: number;
  net_profit_bps: number;
  fee_threshold_bps: number;
  max_qty: number;
  max_notional_usd: number;
  timestamp_ms: number;
  buy_max_leverage: number;
  sell_max_leverage: number;
  buy_min_order_size: number;
  sell_min_order_size: number;
  buy_qty_step: number;
  sell_qty_step: number;
}

/** /ws/arb message: per-position spread snapshot. */
export interface ArbStatusMessage {
  type: "arb_status" | "arb_close";
  token: string;
  buy_exchange: string;
  sell_exchange: string;
  buy_ask: number;
  sell_bid: number;
  spread_bps: number;
  fee_threshold_bps: number;
  profitable: boolean;
  timestamp_ms: number;
  reason?: string;
}

/** /ws/arb message: opportunity broadcast. */
export interface ArbOpportunityMessage extends ArbOpportunity {
  type: "arb_opportunity";
}

/** /ws/arb attachment persisted across hibernation cycles. */
export interface ArbWsAttachment {
  /** Serialized watched-position keys: "token:buy_exch:sell_exch" */
  watch: string[];
  /** Opportunity subscription filter ("" means not subscribed). */
  oppFilter: {
    subscribed: boolean;
    min_profit_bps: number | null;
    exchanges: string[] | null;
  };
  connected_at: number;
}

// ── Phase E: Quote surfaces ──────────────────────────────────────

/**
 * Quote — one-leg VWAP/depth/slippage calculation, sized to the caller's
 * requested qty or notional. Replaces:
 *   - app/safety.py::walk_book / estimate_fill_price / check_order_book_depth
 *   - app/arbitrage.py::_compute_vwap_limit
 */
export interface Quote {
  exchange: string;
  symbol: string;
  side: "buy" | "sell";
  requested_qty: number;
  requested_notional_usd: number;
  fillable_qty: number;
  unfilled_qty: number;
  best_price: number;
  worst_price: number;
  vwap: number;
  mid_price: number;
  slippage_bps_vs_best: number;
  slippage_bps_vs_mid: number;
  notional_usd: number;
  levels_consumed: number;
  total_levels_on_side: number;
  /** worst_price + buffer_ticks*tick_size for buy; worst_price for sell. */
  limit_price_with_buffer: number;
  buffer_ticks: number;
  min_order_size: number;
  qty_step: number;
  tick_size: number;
  taker_fee_pct: number;
  /** requested_qty rounded down to qty_step. */
  harmonized_qty: number;
  feasible: boolean;
  feasibility_reason:
    | null
    | "no_book"
    | "book_stale"
    | "book_disconnected"
    | "empty_side"
    | "missing_size_input"
    | "qty_below_step"
    | "qty_below_min_order_size"
    | "insufficient_depth";
  book_age_ms: number | null;
  timestamp_ms: number;
}

/**
 * CrossQuote — dual-leg arb pre-trade snapshot. Replaces:
 *   - app/spread_analyzer.py::analyze_cross_venue_spread
 *   - app/dna_bot.py::_harmonize_qty
 * The bot needs only this plus client.create_limit_order to enter.
 */
export interface CrossQuote {
  token: string;
  buy_exchange: string;
  buy_symbol: string;
  sell_exchange: string;
  sell_symbol: string;
  requested_qty: number;
  harmonized_qty: number;
  mid_price: number;
  notional_usd: number;
  buy: Quote;
  sell: Quote;
  bbo_spread_bps: number;
  exec_spread_bps: number;
  slippage_bps_over_bbo: number;
  fee_threshold_bps: number;
  net_profit_bps_after_fees: number;
  profitable: boolean;
  /** Exchange whose qty_step was the coarser (binding) one. */
  min_order_size_binding: string;
  feasible: boolean;
  feasibility_reason: string | null;
  timestamp_ms: number;
}
