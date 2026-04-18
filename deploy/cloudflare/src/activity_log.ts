/**
 * Activity Log — ingest bot activity events into Cloudflare Workers Analytics Engine.
 *
 * Ingest: POST /api/activity/ingest (INGEST_TOKEN auth)
 * Read:   GET /api/activity/logs (session auth — queries via SQL API, future)
 */

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

// ─────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────

interface ActivityEvent {
  ts: number;         // epoch seconds
  event: string;      // e.g. "position_opened", "FILL", "ENGINE"
  message: string;    // human-readable description
  bot_type: string;   // "dna" | "funding_arb"
  bot_id: string;     // e.g. "dna-default", "fn-job-123"
  container: string;  // e.g. "tradeautonom-v3", "ta-user-abc123"
  port: string;       // e.g. "8005", "9001"
  user_id?: string;   // optional user identifier
}

interface ActivityIngestPayload {
  events: ActivityEvent[];
}

interface AnalyticsEngineDataset {
  writeDataPoint(event: {
    blobs?: string[];
    doubles?: number[];
    indexes?: string[];
  }): void;
}

// ─────────────────────────────────────────────────────────────
// Ingest handler
// ─────────────────────────────────────────────────────────────

export async function handleActivityIngest(
  request: Request,
  analyticsEngine: AnalyticsEngineDataset,
): Promise<Response> {
  let payload: ActivityIngestPayload;
  try {
    payload = await request.json() as ActivityIngestPayload;
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }

  const events = payload.events;
  if (!events || !Array.isArray(events) || events.length === 0) {
    return json({ error: "No events provided" }, 400);
  }

  let written = 0;
  for (const evt of events) {
    try {
      analyticsEngine.writeDataPoint({
        blobs: [
          evt.container || "",   // blob1: container name
          evt.port || "",        // blob2: port
          evt.bot_type || "",    // blob3: bot type
          evt.bot_id || "",      // blob4: bot id
          evt.event || "",       // blob5: event type
          (evt.message || "").slice(0, 1024),  // blob6: message (capped)
          evt.user_id || "",     // blob7: user id
        ],
        doubles: [
          evt.ts || Date.now() / 1000,  // double1: timestamp
        ],
        indexes: [
          evt.container || "",   // index1: container (for fast lookups)
        ],
      });
      written++;
    } catch {
      // Skip individual write failures — don't block the batch
    }
  }

  return json({ status: "ok", written, total: events.length });
}

// ─────────────────────────────────────────────────────────────
// Query handler (reads from Analytics Engine SQL API)
// ─────────────────────────────────────────────────────────────

interface AEResponse {
  meta: { name: string; type: string }[];
  data: Record<string, unknown>[];
  rows: number;
  rows_before_limit_at_least: number;
}

export async function handleActivityQuery(
  request: Request,
  cfAccountId: string,
  cfApiToken: string,
): Promise<Response> {
  const url = new URL(request.url);
  const container = url.searchParams.get("container") || "";
  const botType = url.searchParams.get("bot_type") || "";
  const event = url.searchParams.get("event") || "";
  const search = url.searchParams.get("search") || "";
  const from = url.searchParams.get("from") || "";
  const to = url.searchParams.get("to") || "";
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "200"), 1000);

  // Build SQL query — Analytics Engine uses blob1..blob20, double1..double20, timestamp, index1
  const conditions: string[] = [];
  const dataset = "tradeautonom-activity";

  if (container) conditions.push(`blob1 = '${escSql(container)}'`);
  if (botType) conditions.push(`blob3 = '${escSql(botType)}'`);
  if (event) conditions.push(`blob5 = '${escSql(event)}'`);
  if (search) conditions.push(`blob6 LIKE '%${escSql(search)}%'`);
  if (from) conditions.push(`timestamp >= '${escSql(from)}'`);
  if (to) conditions.push(`timestamp <= '${escSql(to)}'`);

  const whereClause = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
  const sql = `SELECT blob1, blob2, blob3, blob4, blob5, blob6, blob7, double1, timestamp FROM "${dataset}" ${whereClause} ORDER BY timestamp DESC LIMIT ${limit}`;

  try {
    const apiUrl = `https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/analytics_engine/sql`;
    const resp = await fetch(apiUrl, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${cfApiToken}`,
        "Content-Type": "text/plain",
      },
      body: sql,
    });

    if (!resp.ok) {
      const errText = await resp.text();
      return json({ error: `Analytics Engine API error: ${resp.status}`, detail: errText.slice(0, 500) }, 502);
    }

    const result = await resp.json() as AEResponse;

    // Map blob fields to meaningful names
    const rows = (result.data || []).map((row) => ({
      container: row.blob1 || "",
      port: row.blob2 || "",
      bot_type: row.blob3 || "",
      bot_id: row.blob4 || "",
      event: row.blob5 || "",
      message: row.blob6 || "",
      user_id: row.blob7 || "",
      timestamp: row.double1 || 0,
      datetime: row.timestamp || "",
    }));

    return json({ rows, count: rows.length });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return json({ error: `Query failed: ${message}` }, 500);
  }
}

function escSql(val: string): string {
  return val.replace(/'/g, "''").replace(/\\/g, "\\\\");
}
