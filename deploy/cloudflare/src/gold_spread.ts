/**
 * Gold-spread history — query handler for the historical PAXG/XAUT spread
 * stored by the OMS-v2 worker in the `tradeautonom-gold-spread` Analytics
 * Engine dataset.
 *
 * The dataset's data points use the schema defined in
 * `deploy/cf-containers/oms-v2/src/lib/gold-spread.ts`:
 *   blobs   = [paxg_symbol, xaut_symbol]
 *   doubles = [paxg_mid, xaut_mid, spread_usd, spread_pct,
 *              paxg_bid, paxg_ask, xaut_bid, xaut_ask]
 *   indexes = ["gold-spread"]
 *
 * Queries are issued against the Cloudflare Analytics Engine SQL API
 * (REST), so this handler lives in the main `tradeautonom` worker that
 * already holds CF_ACCOUNT_ID + CF_API_TOKEN. We do **not** need an
 * `analytics_engine_datasets` binding here — bindings are only required
 * for writes.
 *
 * Endpoint: GET /api/gold-spread/history
 *   Query params:
 *     range:      "1h" | "24h" | "7d" | "30d" | "all"   (default "24h")
 *     resolution: "raw" | "1m" | "5m" | "1h"            (auto if omitted)
 *
 * Returns: { points: SpreadPoint[], range, resolution }
 *
 * Auto-resolution policy:
 *   1h   → raw (≈ 720 points at 5 s throttle)
 *   24h  → 1m  (1 440 points)
 *   7d   → 5m  (2 016 points)
 *   30d  → 1h  (720 points)
 *   all  → 1h  (capped at 90 d by CF retention)
 */

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

interface AEResponse {
  meta: { name: string; type: string }[];
  data: Record<string, unknown>[];
  rows: number;
  rows_before_limit_at_least: number;
}

export interface GoldSpreadPoint {
  ts: number;          // epoch ms
  paxg_mid: number;
  xaut_mid: number;
  /** abs(paxg_mid − xaut_mid) — always positive. */
  spread: number;
  spread_pct: number;
  paxg_bid?: number;
  paxg_ask?: number;
  xaut_bid?: number;
  xaut_ask?: number;
  /** Direction-aware entry exec spread (raw only). */
  exec_spread?: number;
  /** Direction-aware exit exec spread (raw only). */
  exit_exec_spread?: number;
  /** Which token is currently the premium one. Raw rows only; aggregated
   * buckets don't carry a single direction (it can flip within a bucket). */
  direction?: "paxg_premium" | "xaut_premium";
}

type Range = "1h" | "24h" | "7d" | "30d" | "all";
type Resolution = "raw" | "1m" | "5m" | "1h";

const RANGE_HOURS: Record<Range, number | null> = {
  "1h": 1,
  "24h": 24,
  "7d": 24 * 7,
  "30d": 24 * 30,
  "all": null,
};

const DEFAULT_RESOLUTION: Record<Range, Resolution> = {
  "1h": "raw",
  "24h": "1m",
  "7d": "5m",
  "30d": "1h",
  "all": "1h",
};

const RESOLUTION_SECONDS: Record<Resolution, number> = {
  raw: 0,
  "1m": 60,
  "5m": 300,
  "1h": 3600,
};

function asRange(v: string | null): Range {
  if (v && v in RANGE_HOURS) return v as Range;
  return "24h";
}

function asResolution(v: string | null): Resolution | null {
  if (v && v in RESOLUTION_SECONDS) return v as Resolution;
  return null;
}

export async function handleGoldSpreadHistory(
  request: Request,
  cfAccountId: string,
  cfApiToken: string,
): Promise<Response> {
  const url = new URL(request.url);
  const range = asRange(url.searchParams.get("range"));
  const resolution = asResolution(url.searchParams.get("resolution")) ?? DEFAULT_RESOLUTION[range];
  const dataset = "tradeautonom-gold-spread";

  // Time filter. Analytics Engine timestamp is a SQL TIMESTAMP; we use
  // intervalSubtract for portability vs hard-coded ISO strings.
  const conditions: string[] = ["index1 = 'gold-spread'"];
  const hours = RANGE_HOURS[range];
  if (hours !== null) {
    conditions.push(`timestamp > NOW() - INTERVAL '${hours}' HOUR`);
  }
  const whereClause = `WHERE ${conditions.join(" AND ")}`;

  // Build SQL: raw rows or aggregated buckets.
  let sql: string;
  const limit = 5000; // CF caps individual queries; 5k covers all our ranges.

  if (resolution === "raw") {
    sql = `
      SELECT
        toUnixTimestamp(timestamp) * 1000 AS ts,
        double1 AS paxg_mid,
        double2 AS xaut_mid,
        double3 AS spread,
        double4 AS spread_pct,
        double5 AS paxg_bid,
        double6 AS paxg_ask,
        double7 AS xaut_bid,
        double8 AS xaut_ask,
        double9 AS exec_spread,
        double10 AS exit_exec_spread,
        blob3 AS direction
      FROM "${dataset}"
      ${whereClause}
      ORDER BY timestamp DESC
      LIMIT ${limit}
    `;
  } else {
    const bucketSec = RESOLUTION_SECONDS[resolution];
    sql = `
      SELECT
        intDiv(toUnixTimestamp(timestamp), ${bucketSec}) * ${bucketSec} * 1000 AS ts,
        avg(double1) AS paxg_mid,
        avg(double2) AS xaut_mid,
        avg(double3) AS spread,
        avg(double4) AS spread_pct,
        avg(double9) AS exec_spread,
        avg(double10) AS exit_exec_spread
      FROM "${dataset}"
      ${whereClause}
      GROUP BY ts
      ORDER BY ts DESC
      LIMIT ${limit}
    `;
  }

  try {
    const apiUrl = `https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/analytics_engine/sql`;
    const resp = await fetch(apiUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cfApiToken}`,
        "Content-Type": "text/plain",
      },
      body: sql,
    });

    if (!resp.ok) {
      const errText = await resp.text();
      return json(
        {
          error: `Analytics Engine API error: ${resp.status}`,
          detail: errText.slice(0, 500),
          sql_preview: sql.trim().slice(0, 500),
        },
        502,
      );
    }

    const result = (await resp.json()) as AEResponse;
    // Sort ascending for chart consumption.
    const points: GoldSpreadPoint[] = (result.data || [])
      .map((row) => ({
        ts: Number(row.ts) || 0,
        paxg_mid: Number(row.paxg_mid) || 0,
        xaut_mid: Number(row.xaut_mid) || 0,
        spread: Number(row.spread) || 0,
        spread_pct: Number(row.spread_pct) || 0,
        ...(row.paxg_bid !== undefined ? { paxg_bid: Number(row.paxg_bid) } : {}),
        ...(row.paxg_ask !== undefined ? { paxg_ask: Number(row.paxg_ask) } : {}),
        ...(row.xaut_bid !== undefined ? { xaut_bid: Number(row.xaut_bid) } : {}),
        ...(row.xaut_ask !== undefined ? { xaut_ask: Number(row.xaut_ask) } : {}),
        ...(row.exec_spread !== undefined ? { exec_spread: Number(row.exec_spread) } : {}),
        ...(row.exit_exec_spread !== undefined ? { exit_exec_spread: Number(row.exit_exec_spread) } : {}),
        ...(row.direction !== undefined && (row.direction === "paxg_premium" || row.direction === "xaut_premium")
          ? { direction: row.direction as "paxg_premium" | "xaut_premium" } : {}),
      }))
      .sort((a, b) => a.ts - b.ts);

    return json({
      points,
      count: points.length,
      range,
      resolution,
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return json({ error: `Query failed: ${message}` }, 500);
  }
}
