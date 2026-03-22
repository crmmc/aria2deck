/**
 * Client-side password hashing for zero-knowledge authentication
 *
 * Flow: password -> PBKDF2(password, SHA256(username), 10000) -> hex string
 */

const PBKDF2_ITERATIONS = 10000;
const DERIVED_KEY_BYTES = 32;

/**
 * Hash password with username-derived salt
 * @param password - User's plaintext password
 * @param username - Username (used to derive salt)
 * @returns Hex-encoded hash string (64 characters)
 */
export async function hashPassword(password: string, username: string): Promise<string> {
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
