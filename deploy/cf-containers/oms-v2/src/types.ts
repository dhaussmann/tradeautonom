/**
 * Shared types for V2 OMS.
 */

import type { ExtendedOms } from "./exchanges/extended";
import type { AggregatorDO } from "./aggregator";

export interface Env {
  EXTENDED_OMS: DurableObjectNamespace<ExtendedOms>;
  AGGREGATOR_DO: DurableObjectNamespace<AggregatorDO>;
}

export interface BookSnapshot {
  exchange: string;
  symbol: string;
  bids: Array<[number, number]>; // sorted descending by price
  asks: Array<[number, number]>; // sorted ascending by price
  timestamp_ms: number;           // server-provided timestamp from exchange message
  connected: boolean;
  updates: number;
  last_seq: number;
}

/** Bot-client WS protocol messages — matches V1 Photon OMS wire format verbatim. */
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

/** Per-WebSocket state persisted via serializeAttachment across hibernation. */
export interface WsAttachment {
  subs: string[]; // list of "exchange:symbol" keys this WS is subscribed to
  connected_at: number;
}
