/**
 * TradeAutonom Cloudflare Worker
 *
 * - Serves the Vue SPA from Workers KV (static assets)
 * - Authenticates users via better-auth (D1-backed sessions)
 * - Proxies /api/* to the user's Docker container via orchestrator
 * - History API served directly from D1
 * - SSE streams are passed through natively (Workers support streaming)
 */

import { getAssetFromKV } from "@cloudflare/kv-asset-handler";
import {
  handleIngest,
  handleEquityHistory,
  handleSnapshotHistory,
  handleTradesHistory,
  cleanupOldSnapshots,
} from "./history";
import {
  handleJournalIngest,
  handleJournalOrders,
  handleJournalFills,
  handleJournalFunding,
  handleJournalPoints,
  handleJournalPositions,
  handleJournalPairedTrades,
  handleJournalSummary,
} from "./journal";
import { handleExecutionLogIngest, handleExecutionLogQuery } from "./execution_log";
import { handleActivityIngest, handleActivityQuery } from "./activity_log";
import { createAuth } from "./auth";
import { loadSecrets, saveSecrets, hasSecrets, maskKeys, filterUpdates } from "./lib/secrets";
// @ts-expect-error — generated at build time by wrangler sites
import manifestJSON from "__STATIC_CONTENT_MANIFEST";

const assetManifest = JSON.parse(manifestJSON);

interface Env {
  __STATIC_CONTENT: KVNamespace;
  NAS_BACKEND: Fetcher;
  OMS_BACKEND: Fetcher;
  ORCHESTRATOR_ORIGIN: string;
  DB: D1Database;
  INGEST_TOKEN: string;
  BETTER_AUTH_SECRET: string;
  ORCH_TOKEN: string;
  ENCRYPTION_KEY: string;
  ADMIN_EMAILS: string;
  ACTIVITY_LOG: AnalyticsEngineDataset;
  CF_API_TOKEN: string;
  CF_ACCOUNT_ID: string;
}

export default {
  async fetch(
    request: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(request.url);

    // ── Vault status: auto-inject keys from D1 if available ──
    if (url.pathname === "/api/auth/status") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleVaultStatus(request, url, env, session.user.id);
    }

    // ── Legacy vault endpoints: proxy to container (fallback) ──
    if (url.pathname === "/api/auth/unlock" || url.pathname === "/api/auth/setup") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleUserApiProxy(request, url, env, session.user.id);
    }

    // ── Secrets key management: stored in D1 ──────────────────
    if (url.pathname === "/api/secrets/keys") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (request.method === "GET") return handleGetKeys(env, session.user.id);
      if (request.method === "POST") return handleUpdateKeys(request, url, env, session.user.id);
      return jsonResponse({ error: "Method not allowed" }, 405);
    }

    // ── better-auth: /api/auth/* ────────────────────────────
    if (url.pathname.startsWith("/api/auth")) {
      const auth = createAuth(env.DB, env.BETTER_AUTH_SECRET, url.origin);
      return auth.handler(request);
    }

    // ── History ingest: no user auth, uses INGEST_TOKEN ─────
    if (url.pathname === "/api/history/ingest" && request.method === "POST") {
      return handleIngest(request, env.DB, env.INGEST_TOKEN);
    }

    // ── TEMP: Admin secrets audit (INGEST_TOKEN auth) ────────
    if (url.pathname === "/api/admin/secrets-audit" && request.method === "GET") {
      const token = url.searchParams.get("token");
      if (token !== env.INGEST_TOKEN) return jsonResponse({ error: "Forbidden" }, 403);
      return handleSecretsAudit(env.DB, env.ENCRYPTION_KEY);
    }
    // ── TEMP: Admin inject keys into a specific user's container ──
    if (url.pathname === "/api/admin/inject-keys" && request.method === "POST") {
      const token = url.searchParams.get("token");
      if (token !== env.INGEST_TOKEN) return jsonResponse({ error: "Forbidden" }, 403);
      const body = await request.json() as { user_id: string };
      const secrets = await loadSecrets(env.DB, body.user_id, env.ENCRYPTION_KEY);
      if (!secrets) return jsonResponse({ error: "No secrets for user" }, 404);
      const injected = await autoInjectKeys(env, body.user_id, secrets);
      return jsonResponse({ injected, user_id: body.user_id });
    }

    // ── Admin: user management ──────────────────────────────
    if (url.pathname === "/api/admin/users" && request.method === "GET") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      return handleAdminListUsers(env);
    }
    if (url.pathname === "/api/admin/check" && request.method === "GET") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return jsonResponse({ is_admin: isAdmin(session, env) });
    }
    if (url.pathname.startsWith("/api/admin/users/") && request.method === "DELETE") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      const targetUserId = url.pathname.replace("/api/admin/users/", "");
      if (!targetUserId) return jsonResponse({ error: "user_id required" }, 400);
      return handleAdminDeleteUser(env, targetUserId);
    }
    if (url.pathname === "/api/admin/activity" && request.method === "GET") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      return handleActivityQuery(request, env.CF_ACCOUNT_ID, env.CF_API_TOKEN);
    }

    // ── Execution log ingest: no user auth, uses INGEST_TOKEN ──
    if (url.pathname === "/api/execution-log/ingest" && request.method === "POST") {
      return handleExecutionLogIngest(request, env.DB, env.INGEST_TOKEN);
    }

    // ── Activity log ingest: no user auth, uses INGEST_TOKEN ──
    if (url.pathname === "/api/activity/ingest" && request.method === "POST") {
      const authHeader = request.headers.get("Authorization") || "";
      const token = authHeader.replace("Bearer ", "");
      if (token !== env.INGEST_TOKEN) return jsonResponse({ error: "Forbidden" }, 403);
      return handleActivityIngest(request, env.ACTIVITY_LOG);
    }

    // ── Journal ingest: no user auth, uses INGEST_TOKEN ──────
    if (url.pathname === "/api/journal/ingest" && request.method === "POST") {
      return handleJournalIngest(request, env.DB, env.INGEST_TOKEN);
    }

    // ── Journal read: requires session ───────────────────────
    if (url.pathname.startsWith("/api/journal/")) {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);

      if (url.pathname === "/api/journal/orders") {
        return handleJournalOrders(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/fills") {
        return handleJournalFills(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/funding") {
        return handleJournalFunding(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/points") {
        return handleJournalPoints(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/positions") {
        return handleJournalPositions(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/paired-trades") {
        return handleJournalPairedTrades(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/journal/summary") {
        return handleJournalSummary(url, env.DB, session.user.id);
      }
    }

    // ── Execution log read: requires session ──────────────────
    if (url.pathname === "/api/execution-log") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleExecutionLogQuery(url, env.DB, session.user.id);
    }

    // ── History read: requires session ──────────────────────
    if (url.pathname.startsWith("/api/history/")) {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);

      if (url.pathname === "/api/history/equity") {
        return handleEquityHistory(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/history/snapshots") {
        return handleSnapshotHistory(url, env.DB, session.user.id);
      }
      if (url.pathname === "/api/history/trades") {
        return handleTradesHistory(url, env.DB, session.user.id);
      }
    }

    // ── OMS proxy: /api/oms/* → OMS service (port 8099) ────
    if (url.pathname.startsWith("/api/oms/") && request.method === "GET") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleOmsProxy(request, url, env);
    }

    // ── Orchestrator admin API: /api/orch/* ──────────────────
    if (url.pathname.startsWith("/api/orch/")) {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleOrchProxy(request, url, env);
    }

    // ── User API proxy: /api/* → user's container ───────────
    if (url.pathname.startsWith("/api/")) {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      return handleUserApiProxy(request, url, env, session.user.id);
    }

    // ── Static assets: serve Vue SPA from KV ─────────────────
    return handleStaticAssets(request, env, ctx, url);
  },

  // ── Cron: daily retention cleanup ────────────────────────
  async scheduled(_controller: ScheduledController, env: Env, _ctx: ExecutionContext) {
    const deleted = await cleanupOldSnapshots(env.DB, 90);
    console.log(`Retention cleanup: deleted ${deleted} old snapshot rows`);
  },
} satisfies ExportedHandler<Env>;

// ─────────────────────────────────────────────────────────────
// Auth helpers
// ─────────────────────────────────────────────────────────────

interface SessionResult {
  user: { id: string; email: string; name: string };
  session: { id: string; token: string };
}

async function getSession(request: Request, env: Env): Promise<SessionResult | null> {
  try {
    const auth = createAuth(env.DB, env.BETTER_AUTH_SECRET, new URL(request.url).origin);
    const session = await auth.api.getSession({ headers: request.headers });
    if (!session?.user) return null;
    return session as SessionResult;
  } catch {
    return null;
  }
}

function isAdmin(session: SessionResult, env: Env): boolean {
  const admins = (env.ADMIN_EMAILS || "").split(",").map((e: string) => e.trim().toLowerCase());
  return admins.includes(session.user.email.toLowerCase());
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

// ─────────────────────────────────────────────────────────────
// User API Proxy — routes to user's container via orchestrator
// ─────────────────────────────────────────────────────────────

async function handleUserApiProxy(
  request: Request,
  url: URL,
  env: Env,
  userId: string,
): Promise<Response> {
  // Look up user's container port
  let row = await env.DB.prepare(
    "SELECT port, status, container_name FROM user_container WHERE user_id = ?"
  ).bind(userId).first<{ port: number; status: string; container_name: string }>();

  // Auto-provision: no container yet → create one via orchestrator
  if (!row) {
    const provision = await provisionContainer(env, userId);
    if ("error" in provision) {
      return jsonResponse(provision, provision.status || 503);
    }
    row = { port: provision.port, status: "running", container_name: provision.container_name };
  }

  if (row.status !== "running") {
    return jsonResponse({
      error: "Container stopped",
      detail: "Your backend container is not running.",
    }, 503);
  }

  // Proxy to user's container via orchestrator, passing port hint for unmanaged containers
  const backendPath = url.pathname.replace(/^\/api/, "") + url.search;
  const targetUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/proxy/${userId}${backendPath}`;

  // Clone request and add port hint header so orchestrator can find the container
  const proxiedRequest = new Request(request.url, request);
  proxiedRequest.headers.set("X-Container-Port", String(row.port));

  return proxyToNas(proxiedRequest, targetUrl, env);
}

// ─────────────────────────────────────────────────────────────
// Auto-provision: create container + wait for healthy
// ─────────────────────────────────────────────────────────────

interface ProvisionResult {
  port: number;
  container_name: string;
}
interface ProvisionError {
  error: string;
  detail: string;
  status: number;
}

async function provisionContainer(
  env: Env,
  userId: string,
): Promise<ProvisionResult | ProvisionError> {
  // Ask orchestrator to create a container
  const createUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/containers`;
  const createReq = new Request(createUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Orch-Token": env.ORCH_TOKEN,
    },
    body: JSON.stringify({ user_id: userId }),
  });

  let createResp: Response;
  try {
    createResp = await env.NAS_BACKEND.fetch(createReq);
  } catch (err) {
    return { error: "Orchestrator unreachable", detail: String(err), status: 502 };
  }

  if (!createResp.ok) {
    const body = await createResp.text();
    return { error: "Container creation failed", detail: body, status: createResp.status };
  }

  const created = await createResp.json() as {
    status: string;
    port: number;
    container_name: string;
  };

  // Save mapping to D1
  await env.DB.prepare(
    "INSERT OR REPLACE INTO user_container (user_id, port, container_name, status) VALUES (?, ?, ?, 'running')"
  ).bind(userId, created.port, created.container_name).run();

  // Wait for container to become healthy (up to ~20 seconds)
  const healthUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/proxy/${userId}/health`;
  for (let i = 0; i < 10; i++) {
    await sleep(2000);
    try {
      const healthReq = new Request(healthUrl, {
        headers: { "X-Orch-Token": env.ORCH_TOKEN },
      });
      const healthResp = await env.NAS_BACKEND.fetch(healthReq);
      if (healthResp.ok) {
        return { port: created.port, container_name: created.container_name };
      }
    } catch {
      // Container not ready yet, keep polling
    }
  }

  // Timed out but container was created — return success anyway,
  // the proxy request might still work or fail gracefully
  return { port: created.port, container_name: created.container_name };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─────────────────────────────────────────────────────────────
// Shared NAS proxy helper
// ─────────────────────────────────────────────────────────────

async function proxyToNas(
  request: Request,
  targetUrl: string,
  env: Env,
): Promise<Response> {
  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.delete("host");
  proxyHeaders.set("X-Orch-Token", env.ORCH_TOKEN);

  const proxyRequest = new Request(targetUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: request.body,
    // @ts-expect-error — duplex is needed for streaming request bodies
    duplex: "half",
  });

  try {
    const response = await env.NAS_BACKEND.fetch(proxyRequest);
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("Access-Control-Allow-Origin", "*");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({
        error: "Backend unreachable",
        detail: err instanceof Error ? err.message : String(err),
      }),
      { status: 502, headers: { "Content-Type": "application/json" } },
    );
  }
}

// ─────────────────────────────────────────────────────────────
// Orchestrator admin proxy: /api/orch/* → orchestrator
// ─────────────────────────────────────────────────────────────

async function handleOrchProxy(
  request: Request,
  url: URL,
  env: Env,
): Promise<Response> {
  const orchPath = url.pathname.replace(/^\/api\/orch/, "/orch") + url.search;
  const orchUrl = `${env.ORCHESTRATOR_ORIGIN}${orchPath}`;

  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.delete("host");
  proxyHeaders.set("X-Orch-Token", env.ORCH_TOKEN);

  const proxyRequest = new Request(orchUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: request.body,
    // @ts-expect-error — duplex is needed for streaming request bodies
    duplex: "half",
  });

  try {
    const response = await env.NAS_BACKEND.fetch(proxyRequest);
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } catch (err) {
    return jsonResponse({ error: "Orchestrator unreachable", detail: String(err) }, 502);
  }
}

// ─────────────────────────────────────────────────────────────
// OMS proxy — routes to OMS service (port 8099) via NAS_BACKEND
// ─────────────────────────────────────────────────────────────

async function handleOmsProxy(
  request: Request,
  url: URL,
  env: Env,
): Promise<Response> {
  const omsPath = url.pathname.replace(/^\/api\/oms/, "") + url.search;
  const omsUrl = `${env.ORCHESTRATOR_ORIGIN.replace(/:\d+$/, "")}:8099${omsPath}`;

  const proxyHeaders = new Headers();
  proxyHeaders.set("Accept", "application/json");

  const proxyRequest = new Request(omsUrl, {
    method: "GET",
    headers: proxyHeaders,
  });

  try {
    const response = await env.OMS_BACKEND.fetch(proxyRequest);
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("Access-Control-Allow-Origin", "*");
    responseHeaders.set("Cache-Control", "no-store");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (err) {
    return jsonResponse({ error: "OMS unreachable", detail: String(err) }, 502);
  }
}

// ─────────────────────────────────────────────────────────────
// Vault status with auto-inject from D1
// ─────────────────────────────────────────────────────────────

async function handleVaultStatus(
  request: Request,
  url: URL,
  env: Env,
  userId: string,
): Promise<Response> {
  // First, proxy the actual /auth/status call to the container
  const statusResp = await handleUserApiProxy(request, url, env, userId);
  if (!statusResp.ok) return statusResp;

  let status: { setup_required?: boolean; locked?: boolean; unlocked?: boolean };
  try {
    status = await statusResp.json() as typeof status;
  } catch {
    return statusResp;
  }

  // If container is unlocked, nothing to do
  if (status.unlocked) {
    return jsonResponse(status);
  }

  // Container is locked or needs setup — try auto-inject from D1
  const secrets = await loadSecrets(env.DB, userId, env.ENCRYPTION_KEY);
  if (!secrets || Object.keys(secrets).length === 0) {
    // No keys in D1 — check if this is a brand-new user
    const d1HasKeys = await hasSecrets(env.DB, userId);
    return jsonResponse({
      ...status,
      d1_has_keys: d1HasKeys,
    });
  }

  // We have keys in D1 — inject them into the container
  const injected = await autoInjectKeys(env, userId, secrets);
  if (injected) {
    return jsonResponse({ setup_required: false, locked: false, unlocked: true, auto_injected: true });
  }

  // Injection failed — return original status so frontend can show manual unlock
  return jsonResponse(status);
}

async function autoInjectKeys(
  env: Env,
  userId: string,
  secrets: Record<string, string>,
): Promise<boolean> {
  // Look up the user's container
  const row = await env.DB.prepare(
    "SELECT port FROM user_container WHERE user_id = ? AND status = 'running'"
  ).bind(userId).first<{ port: number }>();
  if (!row) return false;

  // POST /internal/apply-keys to the container via orchestrator proxy
  const targetUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/proxy/${userId}/internal/apply-keys`;
  const injectReq = new Request(targetUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Orch-Token": env.ORCH_TOKEN,
    },
    body: JSON.stringify({ keys: secrets }),
  });

  try {
    const resp = await env.NAS_BACKEND.fetch(injectReq);
    return resp.ok;
  } catch {
    return false;
  }
}

// ─────────────────────────────────────────────────────────────
// TEMP: Admin secrets audit — check which keys are set per user
// ─────────────────────────────────────────────────────────────

async function handleSecretsAudit(
  db: D1Database,
  encryptionKey: string,
): Promise<Response> {
  // Get all users with secrets
  const rows = await db.prepare(
    `SELECT us.user_id, us.encrypted, u.email, u.name
     FROM user_secrets us
     LEFT JOIN user u ON us.user_id = u.id`
  ).all<{ user_id: string; encrypted: string; email: string | null; name: string | null }>();

  const { decryptSecrets } = await import("./lib/crypto");

  const results: Array<{
    user_id: string;
    email: string | null;
    name: string | null;
    keys_set: string[];
  }> = [];

  for (const row of rows.results ?? []) {
    try {
      const secrets = await decryptSecrets(row.encrypted, encryptionKey);
      const keysSet = Object.entries(secrets)
        .filter(([, v]) => v && v.length > 0)
        .map(([k]) => k);
      results.push({
        user_id: row.user_id,
        email: row.email,
        name: row.name,
        keys_set: keysSet,
      });
    } catch (e) {
      results.push({
        user_id: row.user_id,
        email: row.email,
        name: row.name,
        keys_set: [`ERROR: ${e}`],
      });
    }
  }

  return jsonResponse(results);
}

// ─────────────────────────────────────────────────────────────
// Secrets key management (D1-backed)
// ─────────────────────────────────────────────────────────────

async function handleGetKeys(
  env: Env,
  userId: string,
): Promise<Response> {
  const secrets = await loadSecrets(env.DB, userId, env.ENCRYPTION_KEY);
  if (!secrets) {
    return jsonResponse({ keys: maskKeys({}) });
  }
  return jsonResponse({ keys: maskKeys(secrets) });
}

async function handleUpdateKeys(
  request: Request,
  url: URL,
  env: Env,
  userId: string,
): Promise<Response> {
  let body: Record<string, string>;
  try {
    body = await request.json() as Record<string, string>;
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400);
  }

  // Load existing secrets, merge with updates
  const existing = (await loadSecrets(env.DB, userId, env.ENCRYPTION_KEY)) ?? {};
  const updates = filterUpdates(body);
  if (Object.keys(updates).length === 0) {
    return jsonResponse({ status: "ok", changed: [] });
  }

  const merged = { ...existing, ...updates };
  await saveSecrets(env.DB, userId, merged, env.ENCRYPTION_KEY);

  // Also push to container immediately (if running)
  const injected = await autoInjectKeys(env, userId, merged);

  return jsonResponse({
    status: "ok",
    changed: Object.keys(updates),
    container_updated: injected,
  });
}

// ─────────────────────────────────────────────────────────────
// Admin: user management
// ─────────────────────────────────────────────────────────────

async function handleAdminListUsers(env: Env): Promise<Response> {
  const rows = await env.DB.prepare(
    `SELECT u.id, u.name, u.email, u.createdAt,
            uc.container_name, uc.port, uc.status AS container_status
     FROM user u
     LEFT JOIN user_container uc ON u.id = uc.user_id
     ORDER BY u.createdAt DESC`
  ).all<{
    id: string;
    name: string;
    email: string;
    createdAt: string;
    container_name: string | null;
    port: number | null;
    container_status: string | null;
  }>();

  return jsonResponse({ users: rows.results ?? [] });
}

async function handleAdminDeleteUser(env: Env, userId: string): Promise<Response> {
  // 1. Delete container via orchestrator (best-effort)
  const containerRow = await env.DB.prepare(
    "SELECT container_name FROM user_container WHERE user_id = ?"
  ).bind(userId).first<{ container_name: string }>();

  if (containerRow) {
    try {
      const deleteUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/containers/${userId}`;
      const deleteReq = new Request(deleteUrl, {
        method: "DELETE",
        headers: { "X-Orch-Token": env.ORCH_TOKEN },
      });
      await env.NAS_BACKEND.fetch(deleteReq);
    } catch {
      // Container might already be gone — continue cleanup
    }
  }

  // 2. Delete all D1 rows for this user (cascade)
  await env.DB.batch([
    env.DB.prepare("DELETE FROM user_container WHERE user_id = ?").bind(userId),
    env.DB.prepare("DELETE FROM user_secrets WHERE user_id = ?").bind(userId),
    env.DB.prepare("DELETE FROM session WHERE userId = ?").bind(userId),
    env.DB.prepare("DELETE FROM account WHERE userId = ?").bind(userId),
    env.DB.prepare("DELETE FROM user WHERE id = ?").bind(userId),
  ]);

  return jsonResponse({ status: "deleted", user_id: userId });
}

// ─────────────────────────────────────────────────────────────
// Static Asset Serving (Vue SPA)
// ─────────────────────────────────────────────────────────────

async function handleStaticAssets(
  request: Request,
  env: Env,
  ctx: ExecutionContext,
  url: URL,
): Promise<Response> {
  try {
    // Try to serve the exact asset from KV
    return await getAssetFromKV(
      { request, waitUntil: ctx.waitUntil.bind(ctx) },
      {
        ASSET_NAMESPACE: env.__STATIC_CONTENT,
        ASSET_MANIFEST: assetManifest,
        cacheControl: {
          // Cache hashed assets (JS/CSS) aggressively
          bypassCache: false,
          browserTTL: url.pathname.match(/\.[a-f0-9]{8}\.\w+$/)
            ? 60 * 60 * 24 * 365 // 1 year for hashed assets
            : 0, // no-cache for index.html etc.
        },
      },
    );
  } catch {
    // Asset not found → serve index.html (SPA fallback for Vue Router)
    try {
      const indexRequest = new Request(
        new URL("/index.html", url.origin).toString(),
        request,
      );
      return await getAssetFromKV(
        { request: indexRequest, waitUntil: ctx.waitUntil.bind(ctx) },
        {
          ASSET_NAMESPACE: env.__STATIC_CONTENT,
          ASSET_MANIFEST: assetManifest,
          cacheControl: {
            bypassCache: false,
            browserTTL: 0,
          },
        },
      );
    } catch {
      return new Response("Not Found", { status: 404 });
    }
  }
}
