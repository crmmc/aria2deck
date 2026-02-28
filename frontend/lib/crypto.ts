/**
 * Client-side password hashing for zero-knowledge authentication
 *
 * Flow: password -> PBKDF2(password, SHA256(username), 10000) -> hex string
 */
import CryptoJS from "crypto-js";

const PBKDF2_ITERATIONS = 10000;
const DERIVED_KEY_BYTES = 32;
const DERIVED_KEY_WORDS = DERIVED_KEY_BYTES / 4;

function getSubtleCrypto(): SubtleCrypto | null {
  if (typeof globalThis === "undefined") {
    return null;
  }
  return globalThis.crypto?.subtle ?? null;
}

async function hashWithWebCrypto(
  password: string,
  username: string,
  subtle: SubtleCrypto
): Promise<string> {
  const encoder = new TextEncoder();
  const usernameBytes = encoder.encode(username.toLowerCase());
  const saltBuffer = await subtle.digest("SHA-256", usernameBytes);
  const salt = new Uint8Array(saltBuffer);

  const passwordKey = await subtle.importKey(
    "raw",
    encoder.encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );

  const derivedBits = await subtle.deriveBits(
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

function hashWithCryptoJs(password: string, username: string): string {
  const salt = CryptoJS.SHA256(username.toLowerCase());
  const derived = CryptoJS.PBKDF2(password, salt, {
    keySize: DERIVED_KEY_WORDS,
    iterations: PBKDF2_ITERATIONS,
    hasher: CryptoJS.algo.SHA256,
  });
  return derived.toString(CryptoJS.enc.Hex);
}

/**
 * Hash password with username-derived salt
 * @param password - User's plaintext password
 * @param username - Username (used to derive salt)
 * @returns Hex-encoded hash string (64 characters)
 */
export async function hashPassword(password: string, username: string): Promise<string> {
  const subtle = getSubtleCrypto();
  if (subtle) {
    return hashWithWebCrypto(password, username, subtle);
  }
  return hashWithCryptoJs(password, username);
}
