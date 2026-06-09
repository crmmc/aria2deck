/**
 * Client-side password hashing for zero-knowledge authentication
 *
 * Flow: password -> PBKDF2(password, SHA256(username), 10000) -> hex string
 *
 * Uses crypto.subtle when available (secure contexts), falls back to
 * @noble/hashes for non-secure HTTP contexts (e.g. LAN IP access).
 */

const PBKDF2_ITERATIONS = 10000;
const DERIVED_KEY_BYTES = 32;

function isSubtleAvailable(): boolean {
  return (
    typeof crypto !== "undefined" &&
    typeof crypto.subtle !== "undefined" &&
    typeof crypto.subtle.digest === "function"
  );
}

async function hashWithSubtle(password: string, username: string): Promise<string> {
  const encoder = new TextEncoder();
  const usernameBytes = encoder.encode(username.toLowerCase());
  const saltBuffer = await crypto.subtle.digest("SHA-256", usernameBytes);
  const salt = new Uint8Array(saltBuffer);

  const passwordKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );

  const derivedBits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt,
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    passwordKey,
    DERIVED_KEY_BYTES * 8
  );

  const hashArray = new Uint8Array(derivedBits);
  return Array.from(hashArray)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function hashWithNoble(password: string, username: string): Promise<string> {
  const { sha256 } = await import("@noble/hashes/sha2.js");
  const { pbkdf2 } = await import("@noble/hashes/pbkdf2.js");
  const encoder = new TextEncoder();
  const salt = sha256(encoder.encode(username.toLowerCase()));
  const derived = pbkdf2(sha256, encoder.encode(password), salt, {
    c: PBKDF2_ITERATIONS,
    dkLen: DERIVED_KEY_BYTES,
  });
  return Array.from(derived)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Hash password with username-derived salt
 * @param password - User's plaintext password
 * @param username - Username (used to derive salt)
 * @returns Hex-encoded hash string (64 characters)
 */
export async function hashPassword(password: string, username: string): Promise<string> {
  if (isSubtleAvailable()) {
    return hashWithSubtle(password, username);
  }
  return hashWithNoble(password, username);
}
