/**
 * D1 CRUD for user_secrets table + key management helpers.
 */

import { encryptSecrets, decryptSecrets } from "./crypto";

const MANAGED_KEYS = [
  "extended_api_key",
  "extended_public_key",
  "extended_private_key",
  "extended_vault",
  "grvt_api_key",
  "grvt_private_key",
  "grvt_trading_account_id",
  "variational_jwt_token",
  "nado_private_key",
  "nado_linked_signer_key",
  "nado_wallet_address",
  "nado_subaccount_name",
] as const;

export type SecretKeys = Record<string, string>;

/** Mask a secret value for UI display (show last 4 chars). */
export function maskValue(value: string): string {
  if (!value) return "";
  if (value.length <= 4) return "****";
  return "***" + value.slice(-4);
}

/** Return masked version of all keys for UI display. */
export function maskKeys(keys: SecretKeys): SecretKeys {
  const masked: SecretKeys = {};
  for (const k of MANAGED_KEYS) {
    masked[k] = maskValue(keys[k] ?? "");
  }
  return masked;
}

/** Load + decrypt user secrets from D1. Returns null if no row exists. */
export async function loadSecrets(
  db: D1Database,
  userId: string,
  encryptionKey: string,
): Promise<SecretKeys | null> {
  const row = await db
    .prepare("SELECT encrypted FROM user_secrets WHERE user_id = ?")
    .bind(userId)
    .first<{ encrypted: string }>();
  if (!row) return null;
  return decryptSecrets(row.encrypted, encryptionKey);
}

/** Encrypt + save user secrets to D1. */
export async function saveSecrets(
  db: D1Database,
  userId: string,
  secrets: SecretKeys,
  encryptionKey: string,
): Promise<void> {
  const encrypted = await encryptSecrets(secrets, encryptionKey);
  await db
    .prepare(
      "INSERT OR REPLACE INTO user_secrets (user_id, encrypted, updated_at) VALUES (?, ?, unixepoch())",
    )
    .bind(userId, encrypted)
    .run();
}

/** Check if a user has secrets stored in D1. */
export async function hasSecrets(
  db: D1Database,
  userId: string,
): Promise<boolean> {
  const row = await db
    .prepare("SELECT 1 FROM user_secrets WHERE user_id = ?")
    .bind(userId)
    .first();
  return row !== null;
}

/** Filter an updates dict: only keep managed keys that are non-empty and not masked. */
export function filterUpdates(
  updates: Record<string, string>,
): Record<string, string> {
  const filtered: Record<string, string> = {};
  for (const key of MANAGED_KEYS) {
    const value = updates[key];
    if (value && !value.startsWith("***")) {
      filtered[key] = value;
    }
  }
  return filtered;
}
