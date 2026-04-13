/**
 * AES-256-GCM encryption/decryption using the Web Crypto API.
 * Used to encrypt user API keys before storing in D1.
 *
 * Format: base64( salt[16] | iv[12] | ciphertext+tag )
 */

const SALT_LEN = 16;
const IV_LEN = 12;
const ITERATIONS = 100_000; // PBKDF2 iterations

async function deriveKey(secret: string, salt: Uint8Array): Promise<CryptoKey> {
  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: ITERATIONS, hash: "SHA-256" },
    keyMaterial,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

export async function encryptSecrets(
  secrets: Record<string, string>,
  serverSecret: string,
): Promise<string> {
  const salt = crypto.getRandomValues(new Uint8Array(SALT_LEN));
  const iv = crypto.getRandomValues(new Uint8Array(IV_LEN));
  const key = await deriveKey(serverSecret, salt);
  const plaintext = new TextEncoder().encode(JSON.stringify(secrets));
  const ciphertext = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plaintext),
  );
  // Combine: salt | iv | ciphertext (includes GCM tag)
  const combined = new Uint8Array(SALT_LEN + IV_LEN + ciphertext.length);
  combined.set(salt, 0);
  combined.set(iv, SALT_LEN);
  combined.set(ciphertext, SALT_LEN + IV_LEN);
  return btoa(String.fromCharCode(...combined));
}

export async function decryptSecrets(
  encrypted: string,
  serverSecret: string,
): Promise<Record<string, string>> {
  const combined = Uint8Array.from(atob(encrypted), (c) => c.charCodeAt(0));
  if (combined.length < SALT_LEN + IV_LEN + 1) {
    throw new Error("Encrypted data too short");
  }
  const salt = combined.slice(0, SALT_LEN);
  const iv = combined.slice(SALT_LEN, SALT_LEN + IV_LEN);
  const ciphertext = combined.slice(SALT_LEN + IV_LEN);
  const key = await deriveKey(serverSecret, salt);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv },
    key,
    ciphertext,
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}
