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
  /**
   * Phase F.3: service binding to the `user-v2` Worker (per-user CF
   * Container backend). Routed to when D1 `user.backend = 'cf'`.
   */
  USER_V2: Fetcher;
  ORCHESTRATOR_ORIGIN: string;
  DB: D1Database;
  INGEST_TOKEN: string;
  BETTER_AUTH_SECRET: string;
  ORCH_TOKEN: string;
  /**
   * Shared secret used to authenticate calls from this Worker to the
   * user-v2 Worker. Set via `wrangler secret put V2_SHARED_TOKEN` on BOTH
   * Workers (same value).
   */
  V2_SHARED_TOKEN: string;
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

    // ── TEMP: Admin recycle V2 container — stops the user's Container DO
    //    so the next request cold-starts with fresh envVars. Useful after
    //    changing envVars in user-container.ts (e.g. toggling
    //    EXTENDED_BUILDER_ENABLED) because `wrangler deploy` of Worker
    //    config doesn't auto-restart warm Container instances.
    //    Usage: POST /api/admin/recycle/<user_id>?token=INGEST_TOKEN
    if (url.pathname.startsWith("/api/admin/recycle/") && request.method === "POST") {
      const token = url.searchParams.get("token");
      if (token !== env.INGEST_TOKEN) return jsonResponse({ error: "Forbidden" }, 403);
      const recycleMatch = url.pathname.match(/^\/api\/admin\/recycle\/([^/]+)\/?$/);
      if (!recycleMatch) return jsonResponse({ error: "Invalid recycle path" }, 400);
      const targetUserId = recycleMatch[1]!;
      const recycleReq = new Request(
        `http://user-v2.internal/admin/recycle/${encodeURIComponent(targetUserId)}`,
        {
          method: "POST",
          headers: {
            "X-Internal-Token": env.V2_SHARED_TOKEN || "",
          },
        },
      );
      try {
        const resp = await env.USER_V2.fetch(recycleReq);
        const body = await resp.text();
        return new Response(body, {
          status: resp.status,
          headers: { "content-type": "application/json" },
        });
      } catch (err) {
        return jsonResponse(
          {
            error: "recycle_failed",
            detail: err instanceof Error ? err.message : String(err),
          },
          502,
        );
      }
    }

    // ── TEMP: Admin probe — proxy a GET or POST to the user's current backend.
    //    Used for debugging V2 routing without needing a session cookie.
    //    Usage: GET  /api/admin/probe/<user_id>/<backend_path>?token=...
    //           POST /api/admin/probe/<user_id>/<backend_path>?token=... (body forwarded)
    //    Respects user.backend so you can verify V1 vs V2 routes.
    if (url.pathname.startsWith("/api/admin/probe/")) {
      const token = url.searchParams.get("token");
      if (token !== env.INGEST_TOKEN) return jsonResponse({ error: "Forbidden" }, 403);
      const match = url.pathname.match(/^\/api\/admin\/probe\/([^/]+)(\/.*)?$/);
      if (!match) return jsonResponse({ error: "Invalid probe path" }, 400);
      const probeUserId = match[1]!;
      const probePath = match[2] ?? "/health";
      const backend = await getUserBackend(env, probeUserId);
      const forwardedUrl = new URL(request.url);
      forwardedUrl.pathname = "/api" + probePath;
      forwardedUrl.search = "";
      const forwardedHeaders = new Headers();
      const ct = request.headers.get("content-type");
      if (ct) forwardedHeaders.set("content-type", ct);
      const fakeRequest = new Request(forwardedUrl.toString(), {
        method: request.method,
        headers: forwardedHeaders,
        body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
        // @ts-expect-error — duplex needed for streaming request bodies
        duplex: "half",
      });
      const resp = await handleUserApiProxy(fakeRequest, new URL(fakeRequest.url), env, probeUserId);
      const body = await resp.text();
      return new Response(JSON.stringify({
        user_id: probeUserId,
        backend_resolved: backend,
        backend_path: probePath,
        upstream_method: request.method,
        upstream_status: resp.status,
        upstream_content_type: resp.headers.get("content-type"),
        upstream_body_preview: body.slice(0, 2000),
      }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
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
    // Phase F.3: per-user V1/V2 backend toggle.
    //   POST /api/admin/user/:id/backend  { backend: "photon" | "cf", force?: bool }
    if (url.pathname.match(/^\/api\/admin\/user\/[^/]+\/backend$/) && request.method === "POST") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      const targetUserId = url.pathname.match(/^\/api\/admin\/user\/([^/]+)\/backend$/)![1]!;
      return handleAdminSetBackend(request, env, targetUserId);
    }
    // Phase F.4 M5: one-click migrate V1 → V2 (full state copy + flip).
    //   POST /api/admin/migrate-to-cf/:user_id    { force?: bool }
    if (url.pathname.match(/^\/api\/admin\/migrate-to-cf\/[^/]+$/) && request.method === "POST") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      const targetUserId = url.pathname.match(/^\/api\/admin\/migrate-to-cf\/([^/]+)$/)![1]!;
      return handleAdminMigrateToCf(request, env, targetUserId);
    }
    // Phase F.4 M5: one-click rollback V2 → V1 (force-flush, copy back, flip).
    //   POST /api/admin/migrate-to-photon/:user_id    { force?: bool }
    if (url.pathname.match(/^\/api\/admin\/migrate-to-photon\/[^/]+$/) && request.method === "POST") {
      const session = await getSession(request, env);
      if (!session) return jsonResponse({ error: "Unauthorized" }, 401);
      if (!isAdmin(session, env)) return jsonResponse({ error: "Forbidden" }, 403);
      const targetUserId = url.pathname.match(/^\/api\/admin\/migrate-to-photon\/([^/]+)$/)![1]!;
      return handleAdminMigrateToPhoton(request, env, targetUserId);
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
// User backend resolver — Phase F.3 routing dispatcher
// ─────────────────────────────────────────────────────────────

type UserBackend = "photon" | "cf";

/**
 * Read `user.backend` from D1. Defaults to "photon" for users created
 * before migration 0006 applied (the column has a NOT NULL DEFAULT, so
 * this should only happen if the user row is missing entirely).
 */
async function getUserBackend(env: Env, userId: string): Promise<UserBackend> {
  const row = await env.DB.prepare(
    "SELECT backend FROM user WHERE id = ?"
  ).bind(userId).first<{ backend: string }>();
  return row?.backend === "cf" ? "cf" : "photon";
}

// ─────────────────────────────────────────────────────────────
// User API Proxy — routes to user's container (Photon or CF)
// ─────────────────────────────────────────────────────────────

async function handleUserApiProxy(
  request: Request,
  url: URL,
  env: Env,
  userId: string,
): Promise<Response> {
  const backend = await getUserBackend(env, userId);
  if (backend === "cf") {
    return handleUserApiProxyV2(request, url, env, userId);
  }
  return handleUserApiProxyPhoton(request, url, env, userId);
}

/**
 * V2 (Cloudflare Container) path. Forwards the request to the user's
 * Container DO via the USER_V2 service binding. The DO is addressed via
 * the `/u/<user_id>/<path>` convention implemented by the user-v2 Worker.
 *
 * No D1 `user_container` lookup — the Container DO is directly addressable
 * by idFromName(user_id) inside the user-v2 Worker; no port provisioning.
 */
async function handleUserApiProxyV2(
  request: Request,
  url: URL,
  env: Env,
  userId: string,
): Promise<Response> {
  const backendPath = url.pathname.replace(/^\/api/, "") + url.search;
  const targetUrl = `http://user-v2.internal/u/${encodeURIComponent(userId)}${backendPath}`;

  const proxyHeaders = new Headers(request.headers);
  proxyHeaders.delete("host");
  proxyHeaders.set("X-Internal-Token", env.V2_SHARED_TOKEN || "");
  proxyHeaders.set("X-User-Id", userId);

  const proxyRequest = new Request(targetUrl, {
    method: request.method,
    headers: proxyHeaders,
    body: request.body,
    // @ts-expect-error — duplex is needed for streaming request bodies
    duplex: "half",
  });

  try {
    const response = await env.USER_V2.fetch(proxyRequest);
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("Access-Control-Allow-Origin", "*");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch (err) {
    return jsonResponse(
      {
        error: "V2 backend unreachable",
        detail: err instanceof Error ? err.message : String(err),
      },
      502,
    );
  }
}

/**
 * V1 (Photon) path — unchanged from the pre-F.3 implementation. Kept as a
 * separate function so the branch at the top of handleUserApiProxy is
 * obvious.
 */
async function handleUserApiProxyPhoton(
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
  const backend = await getUserBackend(env, userId);
  if (backend === "cf") {
    return autoInjectKeysV2(env, userId, secrets);
  }
  return autoInjectKeysPhoton(env, userId, secrets);
}

async function autoInjectKeysV2(
  env: Env,
  userId: string,
  secrets: Record<string, string>,
): Promise<boolean> {
  const targetUrl = `http://user-v2.internal/u/${encodeURIComponent(userId)}/internal/apply-keys`;
  const injectReq = new Request(targetUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Internal-Token": env.V2_SHARED_TOKEN || "",
      "X-User-Id": userId,
    },
    body: JSON.stringify({ keys: secrets }),
  });
  try {
    const resp = await env.USER_V2.fetch(injectReq);
    return resp.ok;
  } catch {
    return false;
  }
}

async function autoInjectKeysPhoton(
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
    `SELECT u.id, u.name, u.email, u.createdAt, u.backend,
            uc.container_name, uc.port, uc.status AS container_status
     FROM user u
     LEFT JOIN user_container uc ON u.id = uc.user_id
     ORDER BY u.createdAt DESC`
  ).all<{
    id: string;
    name: string;
    email: string;
    createdAt: string;
    backend: string | null;
    container_name: string | null;
    port: number | null;
    container_status: string | null;
  }>();

  return jsonResponse({ users: rows.results ?? [] });
}

/**
 * Admin-only endpoint to flip a user between the V1 (Photon) and V2 (CF)
 * backend. Pre-flight: if the user has any non-IDLE bot on the current
 * backend, refuse unless `force=true` is passed in the body. This is
 * a safety rail; migration tooling in Phase F.4 will orchestrate the
 * state copy before flipping.
 */
async function handleAdminSetBackend(
  request: Request,
  env: Env,
  targetUserId: string,
): Promise<Response> {
  let body: { backend?: string; force?: boolean };
  try {
    body = await request.json() as { backend?: string; force?: boolean };
  } catch {
    return jsonResponse({ error: "Invalid JSON" }, 400);
  }
  const target = body.backend === "cf" ? "cf" : body.backend === "photon" ? "photon" : null;
  if (!target) {
    return jsonResponse({ error: "backend must be 'photon' or 'cf'" }, 400);
  }
  const force = body.force === true;

  // Pre-flight: read current bot state from the user's CURRENT backend.
  // If bots are running, refuse unless forced.
  if (!force) {
    const preflight = await probeBotsIdle(env, targetUserId);
    if (preflight.checked && !preflight.all_idle) {
      return jsonResponse({
        error: "Bots not idle",
        detail: `Refusing to flip backend while bots are in non-IDLE state. Pass force=true to override. active_states=${JSON.stringify(preflight.states)}`,
        preflight,
      }, 409);
    }
  }

  await env.DB.prepare(
    "UPDATE user SET backend = ?, updatedAt = ? WHERE id = ?"
  ).bind(target, new Date().toISOString(), targetUserId).run();

  return jsonResponse({
    status: "ok",
    user_id: targetUserId,
    backend: target,
    forced: force,
  });
}

/**
 * Ask the user's currently-active backend for /fn/bots and check whether
 * any reported bot has state != IDLE. Used as a guard rail before the
 * backend flip. Best-effort: network errors or missing container count as
 * "checked: false" so the flip still proceeds if the backend is unreachable.
 */
async function probeBotsIdle(
  env: Env,
  userId: string,
): Promise<{ checked: boolean; all_idle: boolean; states: string[] }> {
  try {
    const backend = await getUserBackend(env, userId);
    let resp: Response;
    if (backend === "cf") {
      const url = `http://user-v2.internal/u/${encodeURIComponent(userId)}/fn/bots`;
      resp = await env.USER_V2.fetch(new Request(url, {
        headers: {
          "X-Internal-Token": env.V2_SHARED_TOKEN || "",
          "X-User-Id": userId,
        },
      }));
    } else {
      const url = `${env.ORCHESTRATOR_ORIGIN}/orch/proxy/${userId}/fn/bots`;
      resp = await env.NAS_BACKEND.fetch(new Request(url, {
        headers: { "X-Orch-Token": env.ORCH_TOKEN },
      }));
    }
    if (!resp.ok) return { checked: false, all_idle: true, states: [] };
    const data = await resp.json() as { bots?: Array<{ state?: string }> };
    const states = (data.bots ?? []).map((b) => String(b.state ?? "")).filter(Boolean);
    const all_idle = states.every((s) => s === "IDLE" || s === "ERROR" || s === "" || s === "HOLDING");
    return { checked: true, all_idle, states };
  } catch {
    return { checked: false, all_idle: true, states: [] };
  }
}

/**
 * Phase F.4 M5: V1 (Photon) → V2 (CF) migration orchestrator.
 *
 * Atomic flip with rollback semantics. Steps:
 *   1. User must currently be on backend='photon' in D1
 *   2. All Photon bots must be IDLE/HOLDING/ERROR (use force=true to skip)
 *   3. GET orchestrator's /orch/export-state/<id> — streams /app/data/ tar.gz
 *   4. POST that tarball to user-v2 /__state/flush?user_id=<id>
 *   5. Verify R2 round-trip (GET /__state/restore, must return non-empty)
 *   6. UPDATE user.backend = 'cf' in D1
 *   7. Stop Photon container (best-effort — leaves data volume intact)
 *   8. Recycle the user-v2 Container DO so it cold-starts onto R2 state
 *
 * On failure in any step: bail out and DON'T touch D1, leaving the user
 * on Photon. Local R2 object may remain (overwritten on next migration);
 * Photon container always stays running.
 */
async function handleAdminMigrateToCf(
  request: Request,
  env: Env,
  targetUserId: string,
): Promise<Response> {
  const trace: string[] = [];
  const log = (msg: string) => { trace.push(msg); console.log(`migrate-to-cf[${targetUserId}]: ${msg}`); };

  let body: { force?: boolean };
  try {
    body = await request.json() as { force?: boolean };
  } catch {
    body = {};
  }
  const force = body.force === true;

  try {
    // Step 1: verify user is on Photon
    const userRow = await env.DB.prepare(
      "SELECT id, email, backend FROM user WHERE id = ?"
    ).bind(targetUserId).first<{ id: string; email: string; backend: string | null }>();
    if (!userRow) {
      return jsonResponse({ error: "User not found", trace }, 404);
    }
    const currentBackend = userRow.backend ?? "photon";
    log(`current backend: ${currentBackend}`);
    if (currentBackend === "cf") {
      return jsonResponse({
        error: "Already on V2",
        detail: `User ${userRow.email} is already on backend='cf'. Nothing to migrate.`,
        trace,
      }, 409);
    }

    // Step 2: IDLE check
    if (!force) {
      const preflight = await probeBotsIdle(env, targetUserId);
      log(`preflight: checked=${preflight.checked} all_idle=${preflight.all_idle} states=${JSON.stringify(preflight.states)}`);
      if (preflight.checked && !preflight.all_idle) {
        return jsonResponse({
          error: "Bots not idle",
          detail: `Refusing to migrate — at least one Photon bot is mid-execution. Pause/abort it first or pass force=true.`,
          preflight,
          trace,
        }, 409);
      }
    }

    // Step 3: stream tar.gz from orchestrator
    const exportUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/export-state/${encodeURIComponent(targetUserId)}`;
    log(`exporting from orchestrator: ${exportUrl}`);
    const exportResp = await env.NAS_BACKEND.fetch(new Request(exportUrl, {
      headers: { "X-Orch-Token": env.ORCH_TOKEN },
    }));
    if (!exportResp.ok) {
      const errBody = await exportResp.text().catch(() => "");
      return jsonResponse({
        error: "Export failed",
        detail: `Orchestrator returned HTTP ${exportResp.status}: ${errBody.slice(0, 500)}`,
        trace,
      }, 502);
    }
    const tarBytes = await exportResp.arrayBuffer();
    log(`tar size: ${tarBytes.byteLength} bytes`);
    if (tarBytes.byteLength < 100) {
      return jsonResponse({
        error: "Export too small",
        detail: `Orchestrator returned ${tarBytes.byteLength} bytes — implausibly small for a real /app/data snapshot.`,
        trace,
      }, 502);
    }

    // Step 4: POST to user-v2 /__state/flush
    const flushUrl = `http://user-v2.internal/__state/flush?user_id=${encodeURIComponent(targetUserId)}`;
    log(`flushing to R2: ${flushUrl}`);
    const flushResp = await env.USER_V2.fetch(new Request(flushUrl, {
      method: "POST",
      headers: {
        "X-Internal-Token": env.V2_SHARED_TOKEN || "",
        "Content-Type": "application/gzip",
      },
      body: tarBytes,
    }));
    if (!flushResp.ok) {
      const errBody = await flushResp.text().catch(() => "");
      return jsonResponse({
        error: "Flush to R2 failed",
        detail: `user-v2 returned HTTP ${flushResp.status}: ${errBody.slice(0, 500)}`,
        trace,
      }, 502);
    }
    const flushData = await flushResp.json() as { status?: string; size?: number };
    log(`R2 wrote ${flushData.size} bytes`);

    // Step 5: verify R2 has it
    const restoreUrl = `http://user-v2.internal/__state/restore?user_id=${encodeURIComponent(targetUserId)}`;
    const restoreResp = await env.USER_V2.fetch(new Request(restoreUrl, {
      headers: { "X-Internal-Token": env.V2_SHARED_TOKEN || "" },
    }));
    if (!restoreResp.ok) {
      return jsonResponse({
        error: "R2 verification failed",
        detail: `Restore-readback returned HTTP ${restoreResp.status} — uploaded but unreadable. Aborting before D1 flip.`,
        trace,
      }, 502);
    }
    const verifyBytes = (await restoreResp.arrayBuffer()).byteLength;
    log(`verify read-back: ${verifyBytes} bytes`);

    // Step 6: D1 flip
    await env.DB.prepare(
      "UPDATE user SET backend = ?, updatedAt = ? WHERE id = ?"
    ).bind("cf", new Date().toISOString(), targetUserId).run();
    log(`D1 backend flipped to 'cf'`);

    // Step 7: stop Photon container (best-effort — don't fail if it's already gone)
    let photonStopped = false;
    try {
      const stopUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/containers/${encodeURIComponent(targetUserId)}/stop`;
      const stopResp = await env.NAS_BACKEND.fetch(new Request(stopUrl, {
        method: "POST",
        headers: { "X-Orch-Token": env.ORCH_TOKEN },
      }));
      photonStopped = stopResp.ok;
      log(`photon stop: ${stopResp.status}`);
    } catch (err) {
      log(`photon stop error (non-fatal): ${err instanceof Error ? err.message : String(err)}`);
    }

    // Step 8: recycle CF container so first request cold-starts onto R2 state
    let cfRecycled = false;
    try {
      const recycleUrl = `http://user-v2.internal/admin/recycle/${encodeURIComponent(targetUserId)}`;
      const recycleResp = await env.USER_V2.fetch(new Request(recycleUrl, {
        method: "POST",
        headers: { "X-Internal-Token": env.V2_SHARED_TOKEN || "" },
      }));
      cfRecycled = recycleResp.ok;
      log(`cf recycle: ${recycleResp.status}`);
    } catch (err) {
      log(`cf recycle error (non-fatal): ${err instanceof Error ? err.message : String(err)}`);
    }

    return jsonResponse({
      status: "ok",
      user_id: targetUserId,
      email: userRow.email,
      backend: "cf",
      tar_bytes: tarBytes.byteLength,
      r2_verify_bytes: verifyBytes,
      photon_stopped: photonStopped,
      cf_recycled: cfRecycled,
      forced: force,
      trace,
    });
  } catch (err) {
    return jsonResponse({
      error: "Migration failed",
      detail: err instanceof Error ? err.message : String(err),
      trace,
    }, 500);
  }
}

/**
 * Phase F.4 M5: V2 (CF) → V1 (Photon) rollback orchestrator.
 *
 * Inverse of handleAdminMigrateToCf. Steps:
 *   1. User must currently be on backend='cf' in D1
 *   2. All CF bots must be IDLE (force=true to skip)
 *   3. Trigger immediate flush on V2 so R2 has the latest state
 *   4. GET R2 tarball via /__state/restore
 *   5. Ensure Photon container exists (refuse if not — admin must
 *      create via deploy/v3/manage.sh create first)
 *   6. POST tarball to orchestrator's /orch/import-state/<id>
 *      (it stops the container, wipes /app/data, extracts, restarts)
 *   7. UPDATE user.backend = 'photon' in D1
 *   8. Recycle CF container so it loses its in-RAM keys cleanly
 */
async function handleAdminMigrateToPhoton(
  request: Request,
  env: Env,
  targetUserId: string,
): Promise<Response> {
  const trace: string[] = [];
  const log = (msg: string) => { trace.push(msg); console.log(`migrate-to-photon[${targetUserId}]: ${msg}`); };

  let body: { force?: boolean };
  try {
    body = await request.json() as { force?: boolean };
  } catch {
    body = {};
  }
  const force = body.force === true;

  try {
    // Step 1: verify user is on CF
    const userRow = await env.DB.prepare(
      "SELECT id, email, backend FROM user WHERE id = ?"
    ).bind(targetUserId).first<{ id: string; email: string; backend: string | null }>();
    if (!userRow) {
      return jsonResponse({ error: "User not found", trace }, 404);
    }
    const currentBackend = userRow.backend ?? "photon";
    log(`current backend: ${currentBackend}`);
    if (currentBackend !== "cf") {
      return jsonResponse({
        error: "Not on V2",
        detail: `User ${userRow.email} is on backend='${currentBackend}', not 'cf'. Nothing to roll back.`,
        trace,
      }, 409);
    }

    // Step 2: V2 IDLE check
    if (!force) {
      const preflight = await probeBotsIdle(env, targetUserId);
      log(`preflight: checked=${preflight.checked} all_idle=${preflight.all_idle} states=${JSON.stringify(preflight.states)}`);
      if (preflight.checked && !preflight.all_idle) {
        return jsonResponse({
          error: "Bots not idle",
          detail: `Refusing to roll back — at least one CF bot is mid-execution. Pause/abort it first or pass force=true.`,
          preflight,
          trace,
        }, 409);
      }
    }

    // Step 3: force-flush on V2 so R2 is current. We use the proxy endpoint
    // so this hits the user's container's /settings/flush-state debug
    // endpoint (which calls cloud_persistence.flush() synchronously).
    try {
      const flushUrl = `http://user-v2.internal/u/${encodeURIComponent(targetUserId)}/settings/flush-state`;
      const flushResp = await env.USER_V2.fetch(new Request(flushUrl, {
        method: "POST",
        headers: {
          "X-Internal-Token": env.V2_SHARED_TOKEN || "",
          "X-User-Id": targetUserId,
        },
      }));
      log(`pre-rollback v2 flush: ${flushResp.status}`);
    } catch (err) {
      log(`pre-rollback flush error (continuing): ${err instanceof Error ? err.message : String(err)}`);
    }

    // Step 4: download R2 tarball
    const restoreUrl = `http://user-v2.internal/__state/restore?user_id=${encodeURIComponent(targetUserId)}`;
    const restoreResp = await env.USER_V2.fetch(new Request(restoreUrl, {
      headers: { "X-Internal-Token": env.V2_SHARED_TOKEN || "" },
    }));
    if (!restoreResp.ok) {
      return jsonResponse({
        error: "R2 download failed",
        detail: `Restore returned HTTP ${restoreResp.status} — no V2 state to roll back. User may have never been on V2 long enough for a flush.`,
        trace,
      }, 404);
    }
    const tarBytes = await restoreResp.arrayBuffer();
    log(`r2 tarball: ${tarBytes.byteLength} bytes`);
    if (tarBytes.byteLength < 100) {
      return jsonResponse({
        error: "R2 tarball too small",
        detail: `R2 returned ${tarBytes.byteLength} bytes — implausibly small. Aborting before touching Photon.`,
        trace,
      }, 502);
    }

    // Step 5: ensure Photon container exists
    const containerRow = await env.DB.prepare(
      "SELECT container_name FROM user_container WHERE user_id = ?"
    ).bind(targetUserId).first<{ container_name: string }>();
    if (!containerRow) {
      return jsonResponse({
        error: "Photon container missing",
        detail: `No container registered for user ${targetUserId} on Photon. Admin must run 'deploy/v3/manage.sh create ${targetUserId}' first.`,
        trace,
      }, 412);
    }
    log(`photon container: ${containerRow.container_name}`);

    // Step 6: POST tarball to orchestrator import endpoint
    const importUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/import-state/${encodeURIComponent(targetUserId)}`;
    const importResp = await env.NAS_BACKEND.fetch(new Request(importUrl, {
      method: "POST",
      headers: {
        "X-Orch-Token": env.ORCH_TOKEN,
        "Content-Type": "application/gzip",
      },
      body: tarBytes,
    }));
    if (!importResp.ok) {
      const errBody = await importResp.text().catch(() => "");
      return jsonResponse({
        error: "Photon import failed",
        detail: `Orchestrator returned HTTP ${importResp.status}: ${errBody.slice(0, 500)}`,
        trace,
      }, 502);
    }
    log(`photon import OK`);

    // Step 7: D1 flip
    await env.DB.prepare(
      "UPDATE user SET backend = ?, updatedAt = ? WHERE id = ?"
    ).bind("photon", new Date().toISOString(), targetUserId).run();
    log(`D1 backend flipped to 'photon'`);

    // Step 8: start Photon container (orchestrator left it stopped after import)
    let photonStarted = false;
    try {
      const startUrl = `${env.ORCHESTRATOR_ORIGIN}/orch/containers/${encodeURIComponent(targetUserId)}/start`;
      const startResp = await env.NAS_BACKEND.fetch(new Request(startUrl, {
        method: "POST",
        headers: { "X-Orch-Token": env.ORCH_TOKEN },
      }));
      photonStarted = startResp.ok;
      log(`photon start: ${startResp.status}`);
    } catch (err) {
      log(`photon start error (non-fatal): ${err instanceof Error ? err.message : String(err)}`);
    }

    // Step 9: recycle CF container (clear in-RAM state, won't be reached now)
    try {
      const recycleUrl = `http://user-v2.internal/admin/recycle/${encodeURIComponent(targetUserId)}`;
      await env.USER_V2.fetch(new Request(recycleUrl, {
        method: "POST",
        headers: { "X-Internal-Token": env.V2_SHARED_TOKEN || "" },
      }));
      log(`cf recycled`);
    } catch (err) {
      log(`cf recycle error (non-fatal): ${err instanceof Error ? err.message : String(err)}`);
    }

    return jsonResponse({
      status: "ok",
      user_id: targetUserId,
      email: userRow.email,
      backend: "photon",
      tar_bytes: tarBytes.byteLength,
      photon_started: photonStarted,
      forced: force,
      trace,
    });
  } catch (err) {
    return jsonResponse({
      error: "Rollback failed",
      detail: err instanceof Error ? err.message : String(err),
      trace,
    }, 500);
  }
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
