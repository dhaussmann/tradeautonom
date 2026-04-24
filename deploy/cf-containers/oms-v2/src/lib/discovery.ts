/**
 * Auto-discovery: map exchange markets → base tokens, build cross-exchange pairs.
 *
 * Reference: deploy/monitor/monitor_service.py::_discover_pairs
 *
 * Base tokens that appear on >= MIN_EXCHANGES exchanges are kept.
 * Normalization per exchange:
 *   Extended: split "-", skip equity tokens ("AAPL_24_5"), strip 1000-prefix
 *   GRVT:     base field verbatim
 *   Nado:     strip "-PERP", strip "k"-prefix (kPEPE → PEPE, kBONK → BONK)
 *   Variational: ticker field verbatim, build P-TICKER-USDC-{fi} synthetic
 */

import type { DiscoveredPairs } from "../types";

export const MIN_EXCHANGES = 2;

export interface DiscoveryResult {
  /** token → { exchange → symbol } */
  pairs: DiscoveredPairs;
  /** exchange → list of symbols to track on that exchange */
  symbolsByExchange: {
    extended: string[];
    grvt: string[];
    nado: Array<{ symbol: string; product_id: number }>;
    variational: string[];
  };
  /** Per-exchange metadata for arb scanner + Phase E quote endpoints. */
  meta: {
    maxLeverage: Record<string, Record<string, number>>;
    minOrderSize: Record<string, Record<string, number>>;
    qtyStep: Record<string, Record<string, number>>;
    tickSize: Record<string, Record<string, number>>;
  };
}

export async function discoverPairs(): Promise<DiscoveryResult> {
  const maps: Record<string, Record<string, string>> = {
    extended: {},
    grvt: {},
    nado: {},
    variational: {},
  };
  const nadoProductIds: Record<string, number> = {};
  const lev: Record<string, Record<string, number>> = { extended: {}, grvt: {}, nado: {}, variational: {} };
  const mins: Record<string, Record<string, number>> = { extended: {}, grvt: {}, nado: {}, variational: {} };
  const steps: Record<string, Record<string, number>> = { extended: {}, grvt: {}, nado: {}, variational: {} };
  const ticks: Record<string, Record<string, number>> = { extended: {}, grvt: {}, nado: {}, variational: {} };

  const tasks = [
    loadExtended().then(({ markets, metaLev, metaMin, metaStep, metaTick }) => {
      maps.extended = markets;
      lev.extended = metaLev;
      mins.extended = metaMin;
      steps.extended = metaStep;
      ticks.extended = metaTick;
    }).catch((e) => console.warn("Extended discovery failed:", e)),
    loadGrvt().then(({ markets, metaMin, metaStep, metaTick }) => {
      maps.grvt = markets;
      lev.grvt = Object.fromEntries(Object.keys(markets).map((k) => [k, 10]));
      mins.grvt = metaMin;
      steps.grvt = metaStep;
      ticks.grvt = metaTick;
    }).catch((e) => console.warn("GRVT discovery failed:", e)),
    loadNado().then(({ markets, productIds, metaLev, metaMin, metaStep, metaTick }) => {
      maps.nado = markets;
      Object.assign(nadoProductIds, productIds);
      lev.nado = metaLev;
      mins.nado = metaMin;
      steps.nado = metaStep;
      ticks.nado = metaTick;
    }).catch((e) => console.warn("Nado discovery failed:", e)),
    loadVariational().then(({ markets }) => {
      maps.variational = markets;
      // Variational has no per-symbol tick size published; bots use 1 tick
      // = 0.01 historically. Use 0 to signal "unknown" so /quote falls back.
    }).catch((e) => console.warn("Variational discovery failed:", e)),
  ];
  await Promise.all(tasks);

  // Union of base tokens
  const allBases = new Set<string>();
  for (const m of Object.values(maps)) for (const b of Object.keys(m)) allBases.add(b);

  const pairs: DiscoveredPairs = {};
  for (const base of Array.from(allBases).sort()) {
    const found: Record<string, string> = {};
    for (const [exch, m] of Object.entries(maps)) {
      if (m[base]) found[exch] = m[base];
    }
    if (Object.keys(found).length >= MIN_EXCHANGES) {
      pairs[base] = found;
    }
  }

  // Collect per-exchange symbol lists for tracked pairs only.
  const tracked: Record<string, Set<string>> = {
    extended: new Set(),
    grvt: new Set(),
    nado: new Set(),
    variational: new Set(),
  };
  for (const found of Object.values(pairs)) {
    for (const [exch, sym] of Object.entries(found)) {
      tracked[exch]!.add(sym);
    }
  }

  return {
    pairs,
    symbolsByExchange: {
      extended: Array.from(tracked.extended!).sort(),
      grvt: Array.from(tracked.grvt!).sort(),
      nado: Array.from(tracked.nado!)
        .filter((sym) => nadoProductIds[sym] !== undefined)
        .map((sym) => ({ symbol: sym, product_id: nadoProductIds[sym]! }))
        .sort((a, b) => a.symbol.localeCompare(b.symbol)),
      variational: Array.from(tracked.variational!).sort(),
    },
    meta: { maxLeverage: lev, minOrderSize: mins, qtyStep: steps, tickSize: ticks },
  };
}

// ── Per-exchange loaders ────────────────────────────────────────────

async function loadExtended() {
  const resp = await fetch(
    "https://api.starknet.extended.exchange/api/v1/info/markets",
    { headers: { "User-Agent": "tradeautonom-oms-v2-discovery/0.1" } },
  );
  const body = (await resp.json()) as any;
  const markets: Record<string, string> = {};
  const metaLev: Record<string, number> = {};
  const metaMin: Record<string, number> = {};
  const metaStep: Record<string, number> = {};
  const metaTick: Record<string, number> = {};
  for (const m of body?.data ?? []) {
    if (m?.status !== "ACTIVE") continue;
    const name: string = m.name;
    let base = name.split("-")[0]!.toUpperCase();
    if (base.includes("_")) continue; // skip equity tokens like AAPL_24_5
    if (base.startsWith("1000")) base = base.slice(4);
    markets[base] = name;
    const tc = m.tradingConfig ?? {};
    if (tc.maxLeverage) metaLev[base] = Number(tc.maxLeverage);
    if (tc.minOrderSize) metaMin[base] = Number(tc.minOrderSize);
    if (tc.minOrderSizeChange) metaStep[base] = Number(tc.minOrderSizeChange);
    if (tc.minPriceChange) metaTick[base] = Number(tc.minPriceChange);
  }
  return { markets, metaLev, metaMin, metaStep, metaTick };
}

async function loadGrvt() {
  const resp = await fetch(
    "https://market-data.grvt.io/full/v1/all_instruments",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": "tradeautonom-oms-v2-discovery/0.1",
      },
      body: JSON.stringify({ is_active: true }),
    },
  );
  const body = (await resp.json()) as any;
  const markets: Record<string, string> = {};
  const metaMin: Record<string, number> = {};
  const metaStep: Record<string, number> = {};
  const metaTick: Record<string, number> = {};
  for (const i of body?.result ?? []) {
    const inst: string = i?.instrument;
    const base: string = String(i?.base ?? "").toUpperCase();
    if (!inst || !base) continue;
    markets[base] = inst;
    if (i.min_size) {
      metaMin[base] = Number(i.min_size);
      metaStep[base] = Number(i.min_size);
    }
    if (i.tick_size) metaTick[base] = Number(i.tick_size);
  }
  return { markets, metaMin, metaStep, metaTick };
}

async function loadNado() {
  const resp = await fetch("https://gateway.prod.nado.xyz/symbols", {
    headers: {
      "Accept-Encoding": "gzip",
      "User-Agent": "tradeautonom-oms-v2-discovery/0.1",
    },
  });
  const body = (await resp.json()) as any[];
  const markets: Record<string, string> = {};
  const productIds: Record<string, number> = {};
  const metaLev: Record<string, number> = {};
  const metaMin: Record<string, number> = {};
  const metaStep: Record<string, number> = {};
  const metaTick: Record<string, number> = {};
  for (const s of body ?? []) {
    const sym: string = s?.symbol;
    if (!sym || !sym.endsWith("-PERP")) continue;
    let base = sym.replace("-PERP", "").toUpperCase();
    if (base.startsWith("K")) base = base.slice(1); // kPEPE → PEPE
    markets[base] = sym;
    productIds[sym] = Number(s.product_id);
    const ml = s.max_leverage ?? s.maxLeverage;
    metaLev[base] = ml ? Number(ml) : 20;
    const si: string = String(s.size_increment ?? "0");
    if (si && si !== "0") {
      const step = Number(si) / 1e18;
      metaStep[base] = step;
      metaMin[base] = step;
    }
    const ms: string = String(s.min_size ?? "0");
    if (ms && ms !== "0") {
      metaMin[base] = Number(ms) / 1e18;
    }
    // Nado price tick comes as `price_increment_x18` (integer string × 1e18).
    const pi: string = String(s.price_increment_x18 ?? s.price_increment ?? "0");
    if (pi && pi !== "0") {
      metaTick[base] = Number(pi) / 1e18;
    }
  }
  return { markets, productIds, metaLev, metaMin, metaStep, metaTick };
}

async function loadVariational() {
  const resp = await fetch(
    "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats",
    { headers: { "User-Agent": "tradeautonom-oms-v2-discovery/0.1" } },
  );
  const body = (await resp.json()) as any;
  const markets: Record<string, string> = {};
  for (const listing of body?.listings ?? []) {
    const ticker = String(listing?.ticker ?? "").toUpperCase();
    if (!ticker) continue;
    const fi = Number(listing?.funding_interval_s ?? 3600);
    markets[ticker] = `P-${ticker}-USDC-${fi}`;
  }
  return { markets };
}
