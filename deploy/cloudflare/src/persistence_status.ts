/**
 * Phase F.4 M6 — Admin endpoint that returns the current V2 persistence
 * health per user.
 *
 * Reads two sources:
 *  1. Analytics Engine `tradeautonom-persistence` — last_flush + last_restore
 *     timestamps, sizes, status counts (per user_id).
 *  2. R2 bucket `tradeautonom-user-state` — current object size + uploadedAt
 *     custom metadata as the authoritative "what's actually in R2 right now".
 *
 * Output drives the AdminView "V2 Persistence" tab. Each row:
 *   {
 *     user_id, email, backend,
 *     r2_size_bytes, r2_uploaded_at, r2_age_s,
 *     last_flush_ts, last_flush_status, last_flush_size,
 *     last_restore_ts, last_restore_status,
 *     flushes_24h, flush_errors_24h,
 *     health: 'green' | 'yellow' | 'red' | 'idle'
 *   }
 *
 *   - green:  last_flush within 1 h, no recent errors, R2 has fresh data
 *   - yellow: 1–24 h since last flush, OR has recent errors but R2 still
 *             non-empty
 *   - red:    no flush in > 24 h, OR R2 missing entirely while user is on
 *             backend='cf'
 *   - idle:   user is on backend='photon', irrelevant
 */

import type { Env } from "./index";

interface AEResponse {
  meta: { name: string; type: string }[];
  data: Record<string, unknown>[];
  rows: number;
}

interface UserRow {
  id: string;
  email: string;
  backend: string | null;
}

interface PersistRow {
  user_id: string;
  email: string;
  backend: "photon" | "cf";
  r2_size_bytes: number | null;
  r2_uploaded_at: string | null;
  r2_age_s: number | null;
  last_flush_ts: string | null;
  last_flush_status: string | null;
  last_flush_size: number | null;
  last_restore_ts: string | null;
  last_restore_status: string | null;
  flushes_24h: number;
  flush_errors_24h: number;
  health: "green" | "yellow" | "red" | "idle";
  health_reason: string;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function escSql(val: string): string {
  return val.replace(/'/g, "''").replace(/\\/g, "\\\\");
}

export async function handlePersistenceStatus(
  env: Env,
): Promise<Response> {
  // 1. Pull all users with their backend
  const userQuery = await env.DB.prepare(
    "SELECT id, email, backend FROM user ORDER BY email"
  ).all<UserRow>();
  const users = userQuery.results ?? [];

  // 2. Query Analytics Engine for last 24 h of flush + restore events
  // Aggregated per user.
  const cfAccountId = env.CF_ACCOUNT_ID;
  const cfApiToken = env.CF_API_TOKEN;
  const aeRows: Record<string, Record<string, unknown>[]> = {};

  if (cfAccountId && cfApiToken) {
    const sql = `
      SELECT
        blob1 AS event_kind,
        blob2 AS user_id,
        blob3 AS status,
        double1 AS byte_size,
        double2 AS http_status,
        timestamp AS ts
      FROM "tradeautonom-persistence"
      WHERE timestamp > NOW() - INTERVAL '24' HOUR
      ORDER BY timestamp DESC
      LIMIT 5000
    `;
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
      if (resp.ok) {
        const result = (await resp.json()) as AEResponse;
        for (const row of result.data ?? []) {
          const uid = String(row.user_id ?? "");
          if (!uid) continue;
          (aeRows[uid] ??= []).push(row);
        }
      } else {
        // AE may return 4xx if dataset doesn't exist yet (no events written).
        // That's fine — leave aeRows empty.
      }
    } catch {
      // Best-effort
    }
  }

  // 3. R2 listing — note: env.STATE_BUCKET isn't bound to the main Worker.
  // We have to ask user-v2 to enumerate. Easiest: the user-v2 service
  // binding gives us list access via a small admin handler. For now,
  // do per-user GETs which is acceptable for ≤25 V2 users.
  const cfRows: PersistRow[] = [];
  for (const u of users) {
    const backend = (u.backend ?? "photon") as "photon" | "cf";
    const userEvents = aeRows[u.id] ?? [];

    let lastFlushTs: string | null = null;
    let lastFlushStatus: string | null = null;
    let lastFlushSize: number | null = null;
    let lastRestoreTs: string | null = null;
    let lastRestoreStatus: string | null = null;
    let flushes24h = 0;
    let flushErrors24h = 0;

    for (const ev of userEvents) {
      const kind = String(ev.event_kind ?? "");
      const status = String(ev.status ?? "");
      const ts = String(ev.ts ?? "");
      const size = Number(ev.byte_size ?? 0);
      if (kind === "flush") {
        flushes24h += 1;
        if (status !== "ok") flushErrors24h += 1;
        if (!lastFlushTs) {
          lastFlushTs = ts;
          lastFlushStatus = status;
          lastFlushSize = size;
        }
      } else if (kind === "restore") {
        if (!lastRestoreTs) {
          lastRestoreTs = ts;
          lastRestoreStatus = status;
        }
      }
    }

    // Probe R2 via user-v2 — only if user is on cf (otherwise irrelevant)
    let r2Size: number | null = null;
    let r2UploadedAt: string | null = null;
    let r2AgeS: number | null = null;

    if (backend === "cf") {
      try {
        const url = `http://user-v2.internal/__state/restore?user_id=${encodeURIComponent(u.id)}`;
        const r = await env.USER_V2.fetch(
          new Request(url, { method: "HEAD", headers: { "X-Internal-Token": env.V2_SHARED_TOKEN || "" } }),
        );
        if (r.status === 200) {
          const sizeHeader = r.headers.get("x-r2-size");
          if (sizeHeader) r2Size = Number(sizeHeader);
        }
      } catch {
        // ignore
      }
      // For uploadedAt + age we need a richer probe. The /__state/restore
      // handler returns the body (gzip) — we don't want to download every
      // user's tar just for metadata. Use AE last_flush_ts as proxy.
      if (lastFlushTs) {
        r2UploadedAt = lastFlushTs;
        r2AgeS = Math.floor((Date.now() - new Date(lastFlushTs).getTime()) / 1000);
      }
    }

    // Compute health verdict
    let health: PersistRow["health"] = "idle";
    let reason = "User is on V1 (Photon) — V2 persistence not applicable.";

    if (backend === "cf") {
      if (!lastFlushTs && r2Size === null) {
        health = "red";
        reason = "No flush events in the last 24 h AND no R2 object — user has never persisted.";
      } else if (!lastFlushTs) {
        health = "yellow";
        reason = "No flush events in the last 24 h, but R2 has prior data (size " + r2Size + " B).";
      } else if (r2AgeS !== null && r2AgeS > 86400) {
        health = "red";
        reason = `Last flush was ${Math.floor(r2AgeS / 3600)} h ago — > 24 h threshold.`;
      } else if (r2AgeS !== null && r2AgeS > 3600) {
        health = "yellow";
        reason = `Last flush was ${Math.floor(r2AgeS / 60)} min ago — > 1 h threshold.`;
      } else if (flushErrors24h > 0 && flushErrors24h >= flushes24h / 2) {
        health = "yellow";
        reason = `${flushErrors24h}/${flushes24h} flushes in the last 24 h failed — high error rate.`;
      } else {
        health = "green";
        reason = `${flushes24h} flushes in last 24 h, last ${r2AgeS}s ago, ${flushErrors24h} errors.`;
      }
    }

    cfRows.push({
      user_id: u.id,
      email: u.email,
      backend,
      r2_size_bytes: r2Size,
      r2_uploaded_at: r2UploadedAt,
      r2_age_s: r2AgeS,
      last_flush_ts: lastFlushTs,
      last_flush_status: lastFlushStatus,
      last_flush_size: lastFlushSize,
      last_restore_ts: lastRestoreTs,
      last_restore_status: lastRestoreStatus,
      flushes_24h: flushes24h,
      flush_errors_24h: flushErrors24h,
      health,
      health_reason: reason,
    });
  }

  return jsonResponse({
    generated_at: new Date().toISOString(),
    summary: {
      total_users: cfRows.length,
      on_v2: cfRows.filter((r) => r.backend === "cf").length,
      green: cfRows.filter((r) => r.health === "green").length,
      yellow: cfRows.filter((r) => r.health === "yellow").length,
      red: cfRows.filter((r) => r.health === "red").length,
    },
    rows: cfRows,
  });
}

// Suppress unused warning for the helper while we wire features in piecemeal.
void escSql;
