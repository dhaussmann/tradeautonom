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

  // Baseline env vars for every instance. Per-user overrides (USER_ID,
  // R2 credentials) will be injected at `.get()`-time via envVars(...)
  // once we wire the Worker to look up the user.
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
    // V2-CLOUD-PERSISTENCE flag — Phase F.2 will flip this to "1".
    V2_CLOUD_PERSISTENCE: "0",
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
}
