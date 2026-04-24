/**
 * NadoRelayContainer — Cloudflare Container binding for the Nado upstream
 * relay.
 *
 * Why a container? Cloudflare Workers' outbound WebSocket client cannot
 * negotiate `Sec-WebSocket-Extensions: permessage-deflate`, which Nado's
 * gateway enforces (HTTP 403 otherwise). The container runs Node.js with
 * the `ws` library, which handles deflate transparently.
 *
 * One instance, always-on: streaming services don't benefit from sleep.
 * `sleepAfter` is set far in the future so CF keeps the container alive.
 *
 * Used by `NadoOms` DO via a single persistent WebSocket on the container's
 * `/ws` path.
 *
 * Docs:
 *   - docs/v2-oms-cloudflare-native.md
 *   - container/nado-relay/ (image source)
 */

import { Container } from "@cloudflare/containers";
import type { Env } from "./types";

export class NadoRelayContainer extends Container<Env> {
  // Port the Node process listens on inside the container (see
  // container/nado-relay/src/index.ts).
  defaultPort = 8080;

  // The relay is a long-lived streaming service — we never want it to sleep.
  // @cloudflare/containers' time parser only accepts s/m/h suffixes (no "d").
  // 336h = 14 days. NadoOms DO also keeps it warm with a persistent WS.
  sleepAfter = "336h";

  // Launch the container as soon as the Worker starts — don't wait for
  // first request, since NadoOms DO wants to connect right away.
  override onStart(): void {
    console.log(
      JSON.stringify({ evt: "nado_relay_container_started" }),
    );
  }

  override onStop(): void {
    console.warn(
      JSON.stringify({ evt: "nado_relay_container_stopped" }),
    );
  }

  override onError(error: unknown): void {
    console.error(
      JSON.stringify({
        evt: "nado_relay_container_error",
        err: error instanceof Error ? error.message : String(error),
      }),
    );
  }
}
