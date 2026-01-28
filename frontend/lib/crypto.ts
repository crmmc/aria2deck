/**
 * Client-side password hashing for zero-knowledge authentication
 *
 * Flow: password -> PBKDF2(password, SHA256(username), 10000) -> hex string
 */

/**
 * Hash password with username-derived salt
 * @param password - User's plaintext password
 * @param username - Username (used to derive salt)
 * @returns Hex-encoded hash string (64 characters)
 */
export async function hashPassword(password: string, username: string): Promise<string> {
  const encoder = new TextEncoder();

  // Step 1: Derive salt from username using SHA-256
  const usernameBytes = encoder.encode(username.toLowerCase());
  const saltBuffer = await crypto.subtle.digest('SHA-256', usernameBytes);
  const salt = new Uint8Array(saltBuffer);

  // Step 2: Import password as key material
  const passwordKey = await crypto.subtle.importKey(
    'raw',
    encoder.encode(password),
    'PBKDF2',
    false,
    ['deriveBits']
  );

  // Step 3: Derive key using PBKDF2
  const derivedBits = await crypto.subtle.deriveBits(
    {
      name: 'PBKDF2',
      salt: salt,
      iterations: 10000,
      hash: 'SHA-256'
    },
    passwordKey,
    256 // 32 bytes = 256 bits
  );

  // Step 4: Convert to hex string
  const hashArray = new Uint8Array(derivedBits);
  return Array.from(hashArray)
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}
