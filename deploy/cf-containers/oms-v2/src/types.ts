/**
 * Shared types for V2 OMS.
 */

import type { ExtendedOms } from "./exchanges/extended";
import type { GrvtOms } from "./exchanges/grvt";
import type { NadoOms } from "./exchanges/nado";
import type { VariationalOms } from "./exchanges/variational";
import type { AggregatorDO } from "./aggregator";

export interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  GRVT_OMS: DurableObjectNamespace<GrvtOms>;
  NADO_OMS: DurableObjectNamespace<NadoOms>;
  VARIATIONAL_OMS: DurableObjectNamespace<VariationalOms>;
  AGGREGATOR_DO: DurableObjectNamespace<AggregatorDO>;
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
