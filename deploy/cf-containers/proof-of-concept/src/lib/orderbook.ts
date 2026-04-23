/**
 * Minimal orderbook delta application.
 *
 * Extended sends either a SNAPSHOT or a DELTA payload. Both have the same shape
 * per the V5 OMS reference (deploy/monitor/monitor_service.py):
 *
 *   {
 *     type: "SNAPSHOT" | "DELTA",
 *     m: "<market>",           // market name (e.g. "BTC-USD")
 *     b: [[price, qty], ...],  // bids
 *     a: [[price, qty], ...],  // asks
 *     seq: 12345               // sequence number (DELTA only)
 *   }
 *
 * SNAPSHOT replaces the book. DELTA applies per-level updates: qty=0 removes,
 * qty>0 sets. Both sides are kept sorted (bids descending, asks ascending) and
 * truncated to TOP_N levels.
 */

export const TOP_N = 20;

export interface Orderbook {
  bids: Array<[number, number]>; // sorted descending by price
  asks: Array<[number, number]>; // sorted ascending by price
  ts_ms: number; // last update, wall clock ms
  connected: boolean;
  updates: number;
  last_seq: number; // last applied sequence
}

export function emptyBook(): Orderbook {
  return { bids: [], asks: [], ts_ms: 0, connected: false, updates: 0, last_seq: 0 };
}

interface ExtendedMessage {
  type: "SNAPSHOT" | "DELTA";
  m: string;
  b?: Array<[string | number, string | number]>;
  a?: Array<[string | number, string | number]>;
  seq?: number;
}

export function applyExtendedMessage(book: Orderbook, msg: ExtendedMessage): void {
  const bids = (msg.b ?? []).map(([p, q]) => [Number(p), Number(q)] as [number, number]);
  const asks = (msg.a ?? []).map(([p, q]) => [Number(p), Number(q)] as [number, number]);

  if (msg.type === "SNAPSHOT") {
    book.bids = bids.sort((a, b) => b[0] - a[0]).slice(0, TOP_N);
    book.asks = asks.sort((a, b) => a[0] - b[0]).slice(0, TOP_N);
  } else {
    // DELTA: merge per-level
    applyLevels(book.bids, bids, (a, b) => b[0] - a[0]);
    applyLevels(book.asks, asks, (a, b) => a[0] - b[0]);
    book.bids = book.bids.slice(0, TOP_N);
    book.asks = book.asks.slice(0, TOP_N);
  }

  book.ts_ms = Date.now();
  book.updates += 1;
  if (typeof msg.seq === "number") book.last_seq = msg.seq;
}

function applyLevels(
  side: Array<[number, number]>,
  deltas: Array<[number, number]>,
  comparator: (a: [number, number], b: [number, number]) => number,
): void {
  for (const [price, qty] of deltas) {
    const idx = side.findIndex((lv) => lv[0] === price);
    if (qty === 0) {
      if (idx >= 0) side.splice(idx, 1);
    } else if (idx >= 0) {
      side[idx][1] = qty;
    } else {
      side.push([price, qty]);
    }
  }
  side.sort(comparator);
}
