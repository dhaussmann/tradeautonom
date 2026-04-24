/**
 * UserV2PocContainer — Phase F.0 feasibility PoC for UserContainer-v2.
 *
 * A Python container running the V1 `requirements.txt` + a tiny FastAPI that
 * answers feasibility probes (Starknet curve math, curl_cffi vs Variational,
 * OMS-v2 WebSocket reachability, GRVT SDK import). If all four probes return
 * ok=true, Phase F.1 can proceed with confidence that the V1 Python engine
 * will run under Cloudflare Containers.
 *
 * One singleton instance. Always-on so we can probe quickly and reliably.
 * Will be deleted after F.0 decision.
 *
 * See docs/v2-cf-containers-architecture.md for the wider plan.
 */

import { Container } from "@cloudflare/containers";
import type { Env } from "./types";

export class UserV2PocContainer extends Container<Env> {
  // Matches poc_main.py uvicorn port.
  defaultPort = 8000;

  // Keep warm during the PoC so feasibility probes don't time out on cold
  // starts. F.1 will tune this per real-user semantics.
  sleepAfter = "336h"; // 14 days

  override onStart(): void {
    console.log(JSON.stringify({ evt: "user_v2_poc_started" }));
  }

  override onStop(): void {
    console.warn(JSON.stringify({ evt: "user_v2_poc_stopped" }));
  }

  override onError(error: unknown): void {
    console.error(
      JSON.stringify({
        evt: "user_v2_poc_error",
        err: error instanceof Error ? error.message : String(error),
      }),
    );
  }
}
