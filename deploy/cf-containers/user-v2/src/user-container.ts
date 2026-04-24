/**
 * UserContainer — per-user Python trading engine as a Cloudflare Container.
 *
 * This is the V2 equivalent of the `ta-user-<id>` Docker containers on
 * Photon. Everything V1 does (bots, engine, state machine, exchange
 * clients, vault) is identical — only the host changes from Photon Docker
 * to Cloudflare Containers.
 *
 * One DO instance per user (`idFromName(user_id)`). The container binds
 * to port 8000 and serves the full FastAPI surface from app/server.py.
 *
 * Phase F.1: no state persistence. The container is ephemeral; state is
 * lost on cycle. We deploy and smoke-test but don't route real users yet.
 *
 * Phase F.2 will add R2-backed state persistence (app/cloud_persistence.py)
 * so `/app/data/` survives container recycling.
 *
 * Phase F.3 will wire the main Worker (bot.defitool.de) to route requests
 * to the correct user's container based on the D1 `user.backend` flag.
 *
 * See docs/v2-cf-containers-architecture.md.
 */

import { Container } from "@cloudflare/containers";
import type { Env } from "./types";

export class UserContainer extends Container<Env> {
  // Matches uvicorn's default port from main.py (settings.app_port=8000).
  defaultPort = 8000;

  // Idle users' containers sleep after 30 min with no traffic. Wake happens
  // automatically on next request. Exchange WS connections inside the
  // container will drop during sleep; the engine already handles WS
  // reconnects as part of normal operation.
  //
  // When a user has open positions, the engine's periodic state-save will
  // touch /app/data frequently and the container won't idle out; Phase F.2's
  // flush-to-R2 task will keep things active in practice.
  sleepAfter = "30m";

  // Baseline env vars for every instance.
  //
  // NOTE: env vars are GLOBAL for all V2 users because CF Containers
  // `envVars` is static per-class. Per-user overrides (e.g. different
  // builder-id settings or USER_ID) are injected at request time via
  // `/internal/apply-*` HTTP endpoints, not via envVars.
  //
  // USER_ID and V2_SHARED_TOKEN are set at DO construction time in
  // user-v2/src/index.ts via `stub = ns.get(...).withEnvVars({...})` so
  // each Container instance knows which user it belongs to.
  envVars = {
    APP_HOST: "0.0.0.0",
    APP_PORT: "8000",
    APP_RELOAD: "0", // baked code — no uvicorn hot-reload
    // OMS-v2 subscribe URL. V1 containers hardcoded Photon's 8099.
    FN_OPT_SHARED_MONITOR_URL: "https://oms-v2.defitool.de",
    GRVT_ENV: "prod",
    // History ingestion — same as V1 .env.container.
    HISTORY_INGEST_URL: "https://bot.defitool.de/api/history/ingest",
    HISTORY_INGEST_INTERVAL_S: "300",
    // Cloud persistence ON. cloud_persistence.py calls back via HTTPS to
    // the Worker's /__state/* endpoints (the Worker owns the R2 binding).
    V2_CLOUD_PERSISTENCE: "1",
    V2_FLUSH_INTERVAL_S: "30",
    STATE_ENDPOINT: "https://user-v2.defitool.de",
    // Extended builder-code routing disabled for all V2 users until we add
    // a per-user override mechanism.
    EXTENDED_BUILDER_ENABLED: "false",
  };

  override onStart(): void {
    console.log(JSON.stringify({ evt: "user_container_started" }));
  }

  override onStop(): void {
    console.warn(JSON.stringify({ evt: "user_container_stopped" }));
  }

  override onError(error: unknown): void {
    console.error(
      JSON.stringify({
        evt: "user_container_error",
        err: error instanceof Error ? error.message : String(error),
      }),
    );
  }

  /**
   * Track which user this container instance belongs to so each instance
   * starts with the correct USER_ID env var (used by cloud_persistence for
   * R2 object-key scoping). Durable Object state persists across container
   * restarts within the same DO lifetime.
   */
  private _userIdCache: string | null = null;

  /**
   * Override fetch to:
   *   1. Handle the internal `/__recycle` path (stops the container so the
   *      next request cold-starts with fresh envVars).
   *   2. Read the `X-User-Id` header from the first request and remember
   *      it. Used for the per-instance USER_ID envVar.
   *   3. Forward everything else to the Python FastAPI on port 8000.
   */
  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    // Capture user_id from header on first request. Persist to DO storage
    // so we survive container restarts within the same DO lifetime.
    const headerUserId = request.headers.get("X-User-Id");
    if (headerUserId && !this._userIdCache) {
      this._userIdCache = headerUserId;
      await this.ctx.storage.put("user_id", headerUserId);
    }
    if (!this._userIdCache) {
      const stored = await this.ctx.storage.get<string>("user_id");
      if (stored) this._userIdCache = stored;
    }

    if (url.pathname === "/__recycle") {
      try {
        await this.stop();
        console.log(JSON.stringify({ evt: "user_container_recycled" }));
        return new Response(
          JSON.stringify({ status: "stopped", evt: "will_cold_start_on_next_request" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      } catch (err) {
        return new Response(
          JSON.stringify({
            error: "stop_failed",
            detail: err instanceof Error ? err.message : String(err),
          }),
          { status: 500, headers: { "content-type": "application/json" } },
        );
      }
    }
    return super.fetch(request);
  }

  /**
   * Ensure the container is started with per-instance envVars that carry
   * USER_ID + V2_SHARED_TOKEN + other per-user config. Called from the
   * index.ts Worker on FIRST request only — subsequent requests skip the
   * start call because the container is already running with the right env.
   */
  private _startedWithEnv = false;

  async ensureStarted(userId: string, sharedToken: string): Promise<void> {
    if (!this._userIdCache) {
      const stored = await this.ctx.storage.get<string>("user_id");
      this._userIdCache = stored ?? userId;
      if (!stored) await this.ctx.storage.put("user_id", userId);
    }

    // Only start once per DO lifetime. If Worker restarts the DO, this
    // flag resets and we start the container with fresh envVars.
    if (this._startedWithEnv) return;

    try {
      // IMPORTANT: startAndWaitForPorts replaces (not merges) class envVars.
      // We must re-include the class-level defaults so GRVT_ENV, APP_RELOAD,
      // V2_CLOUD_PERSISTENCE, STATE_ENDPOINT, etc. actually reach the
      // container. Without this, cloud_persistence.py sees
      // V2_CLOUD_PERSISTENCE=0 and silently no-ops.
      await this.startAndWaitForPorts(this.defaultPort, undefined, {
        envVars: {
          ...this.envVars,
          USER_ID: this._userIdCache!,
          V2_SHARED_TOKEN: sharedToken,
        },
      });
      this._startedWithEnv = true;
    } catch (err) {
      console.log(JSON.stringify({
        evt: "ensureStarted_noop_or_error",
        err: err instanceof Error ? err.message : String(err),
      }));
      // If the error says "already running" we can consider it started;
      // otherwise leave the flag false so we retry next time.
      const msg = err instanceof Error ? err.message.toLowerCase() : String(err).toLowerCase();
      if (msg.includes("already") || msg.includes("running")) {
        this._startedWithEnv = true;
      }
    }
  }
}
