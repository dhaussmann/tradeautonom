/**
 * VariationalOms — alarm-driven REST poller against Variational's
 * public metadata/stats endpoint.
 *
 * Reference: deploy/monitor/monitor_service.py::_run_variational_poll_all
 *
 * Unlike the other exchanges, Variational does not have a useful WS
 * market-data feed, so we poll every 1.2s. One HTTP request fetches
 * stats for every listed asset, and we build synthetic books from
 * three size-tier notionals (size_1k, size_100k, size_1m).
 *
 * Symbol format (used by bot clients and auto-discovery):
 *   P-{TICKER}-USDC-{funding_interval_s}    e.g. P-BTC-USDC-3600
 *
 * Size field in our stored book is the USD notional (1000/100000/1000000),
 * not the base-asset quantity. Consumers (DNA-bot arb scanner) must be aware.
 */

import { DurableObject } from "cloudflare:workers";
import type { Env, BookSnapshot } from "../types";

const STATS_URL =
  "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats";
const POLL_INTERVAL_MS = 1200;
const SIZE_TIERS: Array<[string, number]> = [
  ["size_1k", 1_000],
  ["size_100k", 100_000],
  ["size_1m", 1_000_000],
];

interface Book {
  bids: Array<[number, number]>;
  asks: Array<[number, number]>;
  ts_ms: number;
  connected: boolean;
  updates: number;
}

function emptyBook(): Book {
  return { bids: [], asks: [], ts_ms: 0, connected: false, updates: 0 };
}

export class VariationalOms extends DurableObject<Env> {
  private books: Map<string, Book> = new Map(); // keyed by P-TICKER-USDC-{fi} synthetic symbol
  private trackedSymbols: Set<string> = new Set();
  private pollFailures = 0;
  private startedAt: number = Date.now();
  private lastPollMs: number | null = null;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      const stored = (await state.storage.get<string[]>("tracked")) ?? [];
      for (const s of stored) this.trackedSymbols.add(s);
      // Prime the alarm so polling starts even if the DO stays idle initially.
      const current = await state.storage.getAlarm();
      if (current === null) {
        await state.storage.setAlarm(Date.now() + 100);
      }
    });
  }

  async getBook(market: string): Promise<BookSnapshot | null> {
    const b = this.books.get(market);
    if (!b) return null;
    return this.toSnapshot(market, b);
  }

  async ensureTracking(markets: string[]): Promise<{ ok: true; added: number }> {
    let added = 0;
    for (const m of markets) {
      if (!this.trackedSymbols.has(m)) {
        this.trackedSymbols.add(m);
        added += 1;
      }
    }
    if (added > 0) {
      await this.ctx.storage.put("tracked", Array.from(this.trackedSymbols));
      // Next alarm will fill in the books on the next poll tick.
    }
    return { ok: true, added };
  }

  async listMarkets(): Promise<string[]> {
    return Array.from(this.books.keys()).sort();
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;
    if (path === "/health") {
      return this.json({
        status: "ok",
        last_poll_ms: this.lastPollMs,
        poll_failures: this.pollFailures,
        tracked_symbols: this.trackedSymbols.size,
        markets_with_data: this.books.size,
        uptime_ms: Date.now() - this.startedAt,
      });
    }
    return this.json({ error: "not found", path }, 404);
  }

  async alarm(): Promise<void> {
    try {
      await this.pollStats();
    } catch (err) {
      this.pollFailures += 1;
      console.warn("Variational poll failed", err instanceof Error ? err.message : err);
    }
    await this.ctx.storage.setAlarm(Date.now() + POLL_INTERVAL_MS);
  }

  private async pollStats(): Promise<void> {
    if (this.trackedSymbols.size === 0) return;
    const resp = await fetch(STATS_URL, {
      headers: { "User-Agent": "tradeautonom-oms-v2/0.1" },
    });
    if (!resp.ok) {
      this.pollFailures += 1;
      return;
    }
    const body = (await resp.json()) as any;
    this.lastPollMs = Date.now();
    const listings: any[] = Array.isArray(body?.listings) ? body.listings : [];

    // Index by synthetic symbol we care about.
    const trackedByTicker = new Map<string, string[]>();
    // Parse trackedSymbols of the form P-TICKER-USDC-{fi} to build a reverse map.
    for (const sym of this.trackedSymbols) {
      const parts = sym.split("-");
      if (parts.length < 3 || parts[0] !== "P") continue;
      const ticker = parts[1]!.toUpperCase();
      if (!trackedByTicker.has(ticker)) trackedByTicker.set(ticker, []);
      trackedByTicker.get(ticker)!.push(sym);
    }

    for (const listing of listings) {
      const ticker = String(listing?.ticker ?? "").toUpperCase();
      const fi = Number(listing?.funding_interval_s ?? 3600);
      const candidates = trackedByTicker.get(ticker);
      if (!candidates) continue;

      // Synthetic symbol format: P-{TICKER}-USDC-{fi}
      const expected = `P-${ticker}-USDC-${fi}`;
      if (!candidates.includes(expected)) continue;

      const quotes = listing?.quotes ?? {};
      const bids: Array<[number, number]> = [];
      const asks: Array<[number, number]> = [];
      for (const [key, notional] of SIZE_TIERS) {
        const q = quotes[key];
        if (!q) continue;
        const bid = Number(q.bid);
        const ask = Number(q.ask);
        if (!Number.isNaN(bid) && bid > 0) bids.push([bid, notional]);
        if (!Number.isNaN(ask) && ask > 0) asks.push([ask, notional]);
      }
      if (bids.length === 0 && asks.length === 0) continue;

      bids.sort((a, b) => b[0] - a[0]);
      asks.sort((a, b) => a[0] - b[0]);

      let book = this.books.get(expected);
      if (!book) {
        book = emptyBook();
        this.books.set(expected, book);
      }
      book.bids = bids;
      book.asks = asks;
      book.ts_ms = Date.now();
      book.connected = true;
      book.updates += 1;
      this.fanOut(this.toSnapshot(expected, book));
    }
  }

  private fanOut(snap: BookSnapshot): void {
    // Parallel fan-out to AggregatorDO (bots) + ArbScannerDO (arb). Variational
    // is excluded from the default arb pair set (see src/lib/arb.ts
    // ARB_EXCHANGES) but we still push so the scanner can cache books in case
    // a future config enables it.
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    const scanner = this.env.ARB_SCANNER.get(
      this.env.ARB_SCANNER.idFromName("singleton"),
    );
    void Promise.allSettled([
      agg.onBookUpdate(snap),
      scanner.onBookUpdate(snap),
    ]);
  }

  private toSnapshot(market: string, book: Book): BookSnapshot {
    return {
      exchange: "variational",
      symbol: market,
      bids: book.bids,
      asks: book.asks,
      timestamp_ms: book.ts_ms,
      connected: book.connected,
      updates: book.updates,
      last_seq: 0,
    };
  }

  private json(data: unknown, status = 200): Response {
    return new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}
