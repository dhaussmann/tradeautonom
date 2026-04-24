/**
 * Shared message types between the NadoOms DO (Worker) and the relay
 * container. Both sides speak JSON over the internal WebSocket.
 */

/** Messages FROM the DO TO the container. */
export type DoToContainerMessage =
  | {
      op: "subscribe";
      product_id: number;
    }
  | {
      op: "unsubscribe";
      product_id: number;
    }
  | {
      /**
       * Full resync — sent on fresh container connect, or when the DO
       * learns of a new set of product_ids. Container replaces its
       * subscribed set and re-subscribes upstream.
       */
      op: "resubscribe_all";
      product_ids: number[];
    };

/** Messages FROM the container TO the DO. */
export type ContainerToDoMessage =
  | {
      type: "hello";
      relay_version: string;
      started_at_ms: number;
    }
  | {
      type: "upstream_connected";
      at_ms: number;
    }
  | {
      type: "upstream_disconnected";
      at_ms: number;
      reason: string;
    }
  | {
      /** Raw Nado JSON envelope (typically book_depth events). */
      type: "event";
      at_ms: number;
      event: unknown;
    };
