/**
 * Minimal orderbook delta application for Extended's public order book stream.
 *
 * Spec reference: https://api.docs.extended.exchange/#order-book-stream
 *
 * Extended sends either a SNAPSHOT or DELTA payload. Envelope shape:
 *
 *   {
 *     ts: 1701563440000,
 *     type: "SNAPSHOT" | "DELTA",
 *     data: {
 *       m: "BTC-USD",
 *       b: [{ p: "25670", q: "0.1", c: "0.3" }, ...],  // bids
 *       a: [{ p: "25770", q: "0.1", c: "0.2" }, ...]   // asks
 *     },
 *     seq: 1
 *   }
 *
 * Per-level fields:
 *   p — price (string)
 *   q — for a SNAPSHOT the absolute size; for a DELTA the CHANGE in size
 *   c — absolute size in both SNAPSHOT and DELTA (this is what we actually want to store)
 *
 * Because `c` is always absolute, applying a DELTA is just a per-price upsert
 * using `c`; we can ignore the `q` field. qty == 0 removes the level.
 *
 * Sequence: "1" is the first snapshot. Subsequent numbers correspond to deltas.
 * If a client receives a sequence out of order, it should reconnect.
 */

export const TOP_N = 20;

export interface Orderbook {
  bids: Array<[number, number]>; // sorted descending by price
  asks: Array<[number, number]>; // sorted ascending by price
  ts_ms: number;                 // envelope `ts`, server-generated
  received_ms: number;           // wall-clock time when our process saw the message
  connected: boolean;
  updates: number;
  last_seq: number;              // last applied sequence
}

export function emptyBook(): Orderbook {
  return {
    bids: [],
    asks: [],
    ts_ms: 0,
    received_ms: 0,
    connected: false,
    updates: 0,
    last_seq: 0,
  };
}

interface ExtendedLevel {
  p: string | number;
  q: string | number;
  c: string | number;
}

export interface ExtendedMessage {
  ts?: number;
  type?: "SNAPSHOT" | "DELTA";
  seq?: number;
  data?: {
    m?: string;
    b?: ExtendedLevel[];
    a?: ExtendedLevel[];
  };
}

/**
 * Returns true if the message was applied, false if ignored (wrong market, wrong shape, seq gap).
 * If the returned value is false AND msg.seq was provided AND != last_seq + 1 for the market,
 * the caller should treat this as a seq gap and reconnect.
 */
export function applyExtendedMessage(book: Orderbook, msg: ExtendedMessage): boolean {
  if (!msg || !msg.data || !msg.type) return false;

  // Sequence gap detection: snapshots reset seq to 1; deltas must increment by exactly 1
  // except when a scheduled one-minute snapshot arrives (type=SNAPSHOT, any seq >= last).
  const isSnapshot = msg.type === "SNAPSHOT";
  if (!isSnapshot && book.last_seq > 0 && typeof msg.seq === "number") {
    if (msg.seq !== book.last_seq + 1) {
      return false;
    }
  }

  const bids = toLevels(msg.data.b);
  const asks = toLevels(msg.data.a);

  if (isSnapshot) {
    // c == absolute size for snapshot; qty=0 levels should not appear but guard anyway.
    book.bids = bids.filter((l) => l[1] > 0).sort((a, b) => b[0] - a[0]).slice(0, TOP_N);
    book.asks = asks.filter((l) => l[1] > 0).sort((a, b) => a[0] - b[0]).slice(0, TOP_N);
  } else {
    // DELTA: c is the absolute size at that price level after the update.
    applyLevels(book.bids, bids, (a, b) => b[0] - a[0]);
    applyLevels(book.asks, asks, (a, b) => a[0] - b[0]);
    book.bids = book.bids.slice(0, TOP_N);
    book.asks = book.asks.slice(0, TOP_N);
  }

  book.ts_ms = msg.ts ?? Date.now();
  book.received_ms = Date.now();
  book.updates += 1;
  if (typeof msg.seq === "number") book.last_seq = msg.seq;
  return true;
}

function toLevels(src: ExtendedLevel[] | undefined): Array<[number, number]> {
  if (!src) return [];
  return src.map((lv) => [Number(lv.p), Number(lv.c)] as [number, number]);
}

function applyLevels(
  side: Array<[number, number]>,
  deltas: Array<[number, number]>,
  comparator: (a: [number, number], b: [number, number]) => number,
): void {
  for (const [price, absSize] of deltas) {
    const idx = side.findIndex((lv) => lv[0] === price);
    if (absSize === 0) {
      if (idx >= 0) side.splice(idx, 1);
    } else if (idx >= 0) {
      side[idx][1] = absSize;
    } else {
      side.push([price, absSize]);
    }
  }
  side.sort(comparator);
}
