/**
 * Cheap per-book-push statistics: mid price + cumulative size/notional.
 *
 * Computed once per `onBookUpdate` in AggregatorDO and attached to every
 * {type:"book"} WS payload so that bots never need to cumsum themselves.
 */

import type { BookSnapshot, BookPushStats } from "../types";

/**
 * Compute mid + running cumulative sums for bids and asks.
 *
 * `bidQtyCumsum[i]` = sum of sizes at levels 0..i of bids.
 * `bidNotionalCumsum[i]` = sum of price*size at levels 0..i of bids.
 * Same shape for asks.
 */
export function computeBookStats(book: BookSnapshot): BookPushStats {
  const bidQty: number[] = [];
  const bidNotional: number[] = [];
  let qAcc = 0;
  let nAcc = 0;
  for (const [price, size] of book.bids) {
    qAcc += size;
    nAcc += price * size;
    bidQty.push(round8(qAcc));
    bidNotional.push(round4(nAcc));
  }

  const askQty: number[] = [];
  const askNotional: number[] = [];
  qAcc = 0;
  nAcc = 0;
  for (const [price, size] of book.asks) {
    qAcc += size;
    nAcc += price * size;
    askQty.push(round8(qAcc));
    askNotional.push(round4(nAcc));
  }

  const bestBid = book.bids.length > 0 ? book.bids[0]![0] : 0;
  const bestAsk = book.asks.length > 0 ? book.asks[0]![0] : 0;
  const mid = bestBid > 0 && bestAsk > 0
    ? (bestBid + bestAsk) / 2
    : (bestBid || bestAsk);

  return {
    mid_price: mid,
    bid_qty_cumsum: bidQty,
    ask_qty_cumsum: askQty,
    bid_notional_cumsum: bidNotional,
    ask_notional_cumsum: askNotional,
  };
}

function round4(v: number): number { return Math.round(v * 1e4) / 1e4; }
function round8(v: number): number { return Math.round(v * 1e8) / 1e8; }
