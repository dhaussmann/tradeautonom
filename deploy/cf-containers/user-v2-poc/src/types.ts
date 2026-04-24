import type { UserV2PocContainer } from "./user-v2-poc-container";

export interface Env {
  USER_V2_POC: DurableObjectNamespace<UserV2PocContainer>;
}
