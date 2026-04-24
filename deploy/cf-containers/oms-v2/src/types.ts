/**
 * Shared types for V2 OMS.
 */

import type { ExtendedOms } from "./exchanges/extended";
import type { GrvtOms } from "./exchanges/grvt";
import type { NadoOms } from "./exchanges/nado";
import type { VariationalOms } from "./exchanges/variational";
import type { AggregatorDO } from "./aggregator";
import type { NadoRelayContainer } from "./nado-relay-container";
import type { ArbScannerDO } from "./arb-scanner";

export interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  GRVT_OMS: DurableObjectNamespace<GrvtOms>;
  NADO_OMS: DurableObjectNamespace<NadoOms>;
  VARIATIONAL_OMS: DurableObjectNamespace<VariationalOms>;
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

export type ClientMessage = ClientSubscribe | ClientUnsubscribe;

export interface ServerSubscribed {
  type: "subscribed";
  exchange: string;
  symbol: string;
}

export interface ServerBookUpdate {
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

export interface WsAttachment {
  subs: string[];
  connected_at: number;
}

/** Per-exchange market metadata collected during auto-discovery. */
export interface MarketMeta {
  maxLeverage: number;
  minOrderSize: number;
  qtyStep: number;
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
