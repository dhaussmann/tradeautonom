/**
 * Orderbook delta application for Extended's order book stream.
 *
 * Spec: https://api.docs.extended.exchange/#order-book-stream
 * Photon OMS reference: deploy/monitor/monitor_service.py::_handle_extended_msg
 *
 * Message envelope:
 *   { ts, type: "SNAPSHOT" | "DELTA", data: { m, b: [{p,q,c}], a: [{p,q,c}] }, seq }
 *
 * Per-level fields:
 *   p — price
 *   q — absolute size on SNAPSHOT, change size on DELTA (per spec)
 *   c — absolute size in both SNAPSHOT and DELTA
 *
 * The Photon OMS parses `q` as absolute (production-stable). We prefer `c`
 * when present and fall back to `q`. Either way we always treat the number
 * as the absolute level size (upsert), with qty=0 meaning removal.
 *
 * Sequence handling: the shared all-markets stream interleaves seqs across
 * markets, so per-market seq continuity cannot be enforced. The one-per-minute
 * SNAPSHOT self-heals any drift. Photon OMS takes the same approach.
 */

export const TOP_N = 10;

export interface Orderbook {
  bids: Array<[number, number]>; // sorted descending by price
  asks: Array<[number, number]>; // sorted ascending by price
  ts_ms: number;
  received_ms: number;
  connected: boolean;
  updates: number;
  last_seq: number;
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
  q?: string | number;
  c?: string | number;
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

export function applyExtendedMessage(book: Orderbook, msg: ExtendedMessage): boolean {
  if (!msg || !msg.data || !msg.type) return false;

  const isSnapshot = msg.type === "SNAPSHOT";
  const bids = toLevels(msg.data.b);
  const asks = toLevels(msg.data.a);

  if (isSnapshot) {
    book.bids = bids.filter((l) => l[1] > 0).sort((a, b) => b[0] - a[0]).slice(0, TOP_N);
    book.asks = asks.filter((l) => l[1] > 0).sort((a, b) => a[0] - b[0]).slice(0, TOP_N);
  } else {
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
  const out: Array<[number, number]> = [];
  for (const lv of src) {
    if (lv.p === undefined || lv.p === null) continue;
    const price = Number(lv.p);
    const size = lv.c !== undefined && lv.c !== null
      ? Number(lv.c)
      : lv.q !== undefined && lv.q !== null
      ? Number(lv.q)
      : 0;
    if (Number.isNaN(price) || Number.isNaN(size)) continue;
    out.push([price, size]);
  }
  return out;
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
