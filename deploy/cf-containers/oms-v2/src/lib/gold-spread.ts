/**
 * Gold-spread tracker — historical PAXG vs XAUT spread on Variational.
 *
 * The OMS already polls both `P-PAXG-USDC-3600` and `P-XAUT-USDC-3600` from
 * the Variational stats endpoint roughly every 1.2 s. Whenever an update for
 * either token arrives in `AggregatorDO.onBookUpdate`, this module is called
 * with the new snapshot. If we already have a fresh book for the *other* gold
 * token cached, we compute the cross-token spread and append a data point to
 * the `tradeautonom-gold-spread` Analytics Engine dataset.
 *
 * Data shape per data point:
 *   blobs   = [paxg_symbol, xaut_symbol, direction]
 *   doubles = [paxg_mid, xaut_mid, spread_usd, spread_pct,
 *              paxg_bid, paxg_ask, xaut_bid, xaut_ask,
 *              exec_spread, exit_exec_spread]
 *   indexes = ["gold-spread"]
 *
 * Everything except the informational `paxg_mid` / `xaut_mid` is computed
 * from real executable bid/ask prices — no mid-based approximations enter
 * the signal path. The chart, the threshold comparisons and the leg
 * assignment all use the exact same numbers a taker would pay.
 *
 * direction          = "paxg_premium" | "xaut_premium" — which side gives a
 *                      positive executable spread (i.e. which is the right
 *                      one to short). Tie-broken to "paxg_premium" when
 *                      both candidates are equal or both negative; in that
 *                      case `spread` is also <= 0 and the bot won't enter.
 * spread             = max(paxg_bid − xaut_ask, xaut_bid − paxg_ask)
 *                      — the best executable cross-token spread *right now*.
 *                      Same number as exec_spread; kept as the "headline"
 *                      value the chart and thresholds use.
 * exec_spread        = entry spread (= spread). Always represented as
 *                      "short the premium token at its bid, long the
 *                      discount token at its ask".
 * exit_exec_spread   = reverse of entry — what closing the position costs
 *                      now (bid−ask the *other* way around). Negative
 *                      while the position is profitable, swings positive
 *                      again on convergence which is the exit signal.
 *
 * exec_spread can briefly go negative when bid/ask costs exceed the
 * cross-token gap — that's expected, it just means "no profitable trade
 * right now". The bot's entry threshold filters those out automatically.
 * spread_pct is the same value normalised by the cheaper-side mid for
 * scale (mids are cheap to keep around for percent display).
 *
 * Write rate-limit: at most one data point per WRITE_THROTTLE_MS to avoid
 * burning the Analytics Engine quota — Variational quotes drift slowly enough
 * that 5 s resolution is plenty for the chart UI.
 *
 * The pair is intentionally hard-coded for the gold convergence bot. If we
 * ever want to track other token pairs we should generalise the throttle map
 * keying instead of duplicating this file.
 */

import type { AnalyticsEngineDataset, BookSnapshot } from "../types";

/**
 * Variational symbols for tokenized gold perps.
 *
 * NOTE: Variational changes the funding-interval suffix periodically (the
 * gold pair currently uses 14400 = 4h, previously 3600 = 1h). The OMS
 * discovery cron always picks up the live suffix, so we accept the union
 * of known variants here and the actual matching is done by underlying
 * token name in `trackGoldSpread()`. Trading code uses
 * `app/symbol_resolver.py::resolve_variational_symbol` which auto-corrects
 * to whatever the OMS currently tracks.
 */
const PAXG_SYMBOLS = new Set<string>([
  "P-PAXG-USDC-14400",
  "P-PAXG-USDC-3600",
]);
const XAUT_SYMBOLS = new Set<string>([
  "P-XAUT-USDC-14400",
  "P-XAUT-USDC-3600",
]);

/** Convenience constants — current canonical form (4h funding interval). */
export const PAXG_SYMBOL = "P-PAXG-USDC-14400";
export const XAUT_SYMBOL = "P-XAUT-USDC-14400";

/** Throttle: don't write more than one data point per this many ms. */
const WRITE_THROTTLE_MS = 5000;
/** Max staleness of the *other* leg's cached book to count as "live". */
const MAX_PAIR_STALENESS_MS = 30_000;

/**
 * Module-level cache of the latest Variational gold-token books. Lives in
 * the Worker isolate which is shared across all `onBookUpdate` invocations
 * for the same DO instance, so consecutive PAXG/XAUT updates can compute
 * a spread without a storage round-trip.
 *
 * Keyed by canonical token name ("PAXG" / "XAUT") rather than full symbol
 * so we don't get confused when Variational rotates the funding-interval
 * suffix mid-run.
 */
const cache = new Map<"PAXG" | "XAUT", BookSnapshot>();
let lastWriteMs = 0;

function classifySymbol(symbol: string): "PAXG" | "XAUT" | null {
  if (PAXG_SYMBOLS.has(symbol)) return "PAXG";
  if (XAUT_SYMBOLS.has(symbol)) return "XAUT";
  return null;
}

function bestBidAsk(book: BookSnapshot): { bid: number; ask: number } | null {
  const bid = book.bids[0]?.[0];
  const ask = book.asks[0]?.[0];
  if (!bid || !ask || bid <= 0 || ask <= 0) return null;
  return { bid, ask };
}

export type GoldSpreadDirection = "paxg_premium" | "xaut_premium";

export interface GoldSpreadDataPoint {
  ts_ms: number;
  paxg_mid: number;
  xaut_mid: number;
  /** abs(paxg_mid − xaut_mid) — always positive, "how far apart". */
  spread: number;
  /** spread / xaut_mid * 100 — always positive. */
  spread_pct: number;
  paxg_bid: number;
  paxg_ask: number;
  xaut_bid: number;
  xaut_ask: number;
  /**
   * Direction-aware entry exec spread. Represents "short the premium
   * token at its bid, long the discount token at its ask". Positive
   * when the bid/ask cost has not yet eaten the mid spread.
   */
  exec_spread: number;
  /** Direction-aware exit exec spread (reverse of entry). */
  exit_exec_spread: number;
  /** Which token currently trades at a premium (mid-based). */
  direction: GoldSpreadDirection;
}

/**
 * Called from `AggregatorDO.onBookUpdate` for every Variational book.
 * No-ops if the symbol is not PAXG/XAUT, the counter-leg book is missing,
 * or the throttle window has not elapsed. Returns the data point that was
 * written (or null) for testing.
 */
export function trackGoldSpread(
  ds: AnalyticsEngineDataset | undefined,
  snap: BookSnapshot,
): GoldSpreadDataPoint | null {
  if (snap.exchange !== "variational") return null;
  const token = classifySymbol(snap.symbol);
  if (!token) return null;

  cache.set(token, snap);

  const paxg = cache.get("PAXG");
  const xaut = cache.get("XAUT");
  if (!paxg || !xaut) return null;

  // Reject stale counter-leg.
  const now = Date.now();
  const minTs = Math.min(paxg.timestamp_ms, xaut.timestamp_ms);
  if (now - minTs > MAX_PAIR_STALENESS_MS) return null;

  const paxgBba = bestBidAsk(paxg);
  const xautBba = bestBidAsk(xaut);
  if (!paxgBba || !xautBba) return null;

  const paxgMid = (paxgBba.bid + paxgBba.ask) / 2;
  const xautMid = (xautBba.bid + xautBba.ask) / 2;
  if (xautMid <= 0) return null;

  // Compute both potential executable entry spreads and pick the larger
  // (= the side that is actually shortable for a profit right now). The
  // direction follows the choice, so leg assignment and signal logic stay
  // perfectly consistent with the chart line.
  const paxgPremiumExec = paxgBba.bid - xautBba.ask;  // short PAXG @ bid, long XAUT @ ask
  const xautPremiumExec = xautBba.bid - paxgBba.ask;  // short XAUT @ bid, long PAXG @ ask
  let direction: GoldSpreadDirection;
  let execSpread: number;
  let exitExecSpread: number;
  if (paxgPremiumExec >= xautPremiumExec) {
    direction = "paxg_premium";
    execSpread = paxgPremiumExec;
    exitExecSpread = xautBba.bid - paxgBba.ask;  // = xautPremiumExec, the reverse direction
  } else {
    direction = "xaut_premium";
    execSpread = xautPremiumExec;
    exitExecSpread = paxgBba.bid - xautBba.ask;  // = paxgPremiumExec
  }

  // The "headline" spread is the executable entry spread. Keeping it as a
  // dedicated field (rather than only exec_spread) lets old chart code and
  // analytics queries that read `spread` keep working without changes.
  const spread = execSpread;
  // Percent normalised by the cheaper-side mid so 0.1% etc. stays scale
  // independent of which token happens to be premium right now.
  const refMid = Math.min(paxgMid, xautMid);
  const spreadPct = refMid > 0 ? (spread / refMid) * 100 : 0;

  const point: GoldSpreadDataPoint = {
    ts_ms: now,
    paxg_mid: paxgMid,
    xaut_mid: xautMid,
    spread,
    spread_pct: spreadPct,
    paxg_bid: paxgBba.bid,
    paxg_ask: paxgBba.ask,
    xaut_bid: xautBba.bid,
    xaut_ask: xautBba.ask,
    exec_spread: execSpread,
    exit_exec_spread: exitExecSpread,
    direction,
  };

  // Throttle Analytics Engine writes. We always recompute (cheap) so callers
  // can read the latest cached point if they want, but we only persist at
  // WRITE_THROTTLE_MS resolution.
  if (!ds) return point;
  if (now - lastWriteMs < WRITE_THROTTLE_MS) return point;
  lastWriteMs = now;

  try {
    ds.writeDataPoint({
      blobs: [paxg.symbol, xaut.symbol, point.direction],
      doubles: [
        point.paxg_mid,         // double1
        point.xaut_mid,         // double2
        point.spread,           // double3 — abs distance
        point.spread_pct,       // double4
        point.paxg_bid,         // double5
        point.paxg_ask,         // double6
        point.xaut_bid,         // double7
        point.xaut_ask,         // double8
        point.exec_spread,      // double9 — direction-aware entry
        point.exit_exec_spread, // double10 — direction-aware exit
      ],
      indexes: ["gold-spread"],
    });
  } catch {
    // Fire-and-forget — never let analytics failures crash a hot path.
  }

  return point;
}

/** Read-only snapshot of the most recent computed point, if any. */
export function getLatestGoldSpread(): GoldSpreadDataPoint | null {
  const paxg = cache.get("PAXG");
  const xaut = cache.get("XAUT");
  if (!paxg || !xaut) return null;
  const paxgBba = bestBidAsk(paxg);
  const xautBba = bestBidAsk(xaut);
  if (!paxgBba || !xautBba) return null;
  const paxgMid = (paxgBba.bid + paxgBba.ask) / 2;
  const xautMid = (xautBba.bid + xautBba.ask) / 2;
  if (xautMid <= 0) return null;
  // Same direction-picking logic as trackGoldSpread() — see that function
  // for the rationale. Kept inline rather than extracted because both
  // call sites are short and the inputs differ slightly (cached vs live).
  const paxgPremiumExec = paxgBba.bid - xautBba.ask;
  const xautPremiumExec = xautBba.bid - paxgBba.ask;
  let direction: GoldSpreadDirection;
  let execSpread: number;
  let exitExecSpread: number;
  if (paxgPremiumExec >= xautPremiumExec) {
    direction = "paxg_premium";
    execSpread = paxgPremiumExec;
    exitExecSpread = xautPremiumExec;
  } else {
    direction = "xaut_premium";
    execSpread = xautPremiumExec;
    exitExecSpread = paxgPremiumExec;
  }
  const refMid = Math.min(paxgMid, xautMid);
  return {
    ts_ms: Math.min(paxg.timestamp_ms, xaut.timestamp_ms),
    paxg_mid: paxgMid,
    xaut_mid: xautMid,
    spread: execSpread,
    spread_pct: refMid > 0 ? (execSpread / refMid) * 100 : 0,
    paxg_bid: paxgBba.bid,
    paxg_ask: paxgBba.ask,
    xaut_bid: xautBba.bid,
    xaut_ask: xautBba.ask,
    exec_spread: execSpread,
    exit_exec_spread: exitExecSpread,
    direction,
  };
}
