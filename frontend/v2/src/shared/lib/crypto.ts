/**
 * 客户端密码派生（ACC-12）：wire 值是与后端 derive_password_hash 同参数的
 * PBKDF2-HMAC-SHA256（盐 = sha256(username.strip().lower()) 的 hex ASCII 字节，10000 轮，256 bit）hex。
 * 非安全上下文（无 crypto.subtle）用等值纯 JS 回退。
 */

const PBKDF2_ITERATIONS = 10000;
const PBKDF2_BITS = 256;
const HMAC_BLOCK_BYTES = 64;

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr(value: number, bits: number): number {
  return ((value >>> bits) | (value << (32 - bits))) >>> 0;
}

function toHex(buffer: ArrayBuffer | Uint8Array): string {
  return Array.from(buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** 纯 JS SHA-256（FIPS 180-4）：非安全上下文（无 crypto.subtle，如纯 HTTP 站点）下的等值回退。 */
function sha256PureJs(bytes: Uint8Array): Uint8Array {
  const length = bytes.length;
  const blockCount = Math.ceil((length + 9) / 64);
  const padded = new Uint8Array(blockCount * 64);
  padded.set(bytes);
  padded[length] = 0x80;
  const view = new DataView(padded.buffer);
  const bitLength = length * 8;
  view.setUint32(padded.length - 8, Math.floor(bitLength / 2 ** 32));
  view.setUint32(padded.length - 4, bitLength >>> 0);

  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const words = new Uint32Array(64);
  for (let block = 0; block < blockCount; block += 1) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(block * 64 + index * 4);
    }
    for (let index = 16; index < 64; index += 1) {
      const previous = words[index - 15];
      const twoAgo = words[index - 2];
      const s0 = rotr(previous, 7) ^ rotr(previous, 18) ^ (previous >>> 3);
      const s1 = rotr(twoAgo, 17) ^ rotr(twoAgo, 19) ^ (twoAgo >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index += 1) {
      const bigS1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + bigS1 + choose + K[index] + words[index]) >>> 0;
      const bigS0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (bigS0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    const round = [a, b, c, d, e, f, g, h];
    for (let index = 0; index < 8; index += 1) {
      state[index] = (state[index] + round[index]) >>> 0;
    }
  }
  const digest = new Uint8Array(32);
  const digestView = new DataView(digest.buffer);
  for (let index = 0; index < 8; index += 1) {
    digestView.setUint32(index * 4, state[index]);
  }
  return digest;
}

export async function sha256Hex(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const subtle = globalThis.crypto?.subtle;
  if (subtle !== undefined) {
    return toHex(await subtle.digest("SHA-256", data));
  }
  return toHex(sha256PureJs(data));
}

/** 纯 JS HMAC-SHA256（RFC 2104）：PBKDF2 回退路径的构件。 */
function hmacSha256(key: Uint8Array, message: Uint8Array): Uint8Array {
  const paddedKey = new Uint8Array(HMAC_BLOCK_BYTES);
  paddedKey.set(key.length > HMAC_BLOCK_BYTES ? sha256PureJs(key) : key);
  const inner = new Uint8Array(HMAC_BLOCK_BYTES + message.length);
  const outer = new Uint8Array(HMAC_BLOCK_BYTES + 32);
  for (let index = 0; index < HMAC_BLOCK_BYTES; index += 1) {
    inner[index] = paddedKey[index] ^ 0x36;
    outer[index] = paddedKey[index] ^ 0x5c;
  }
  inner.set(message, HMAC_BLOCK_BYTES);
  outer.set(sha256PureJs(inner), HMAC_BLOCK_BYTES);
  return sha256PureJs(outer);
}

/** 纯 JS PBKDF2-HMAC-SHA256（RFC 8018）：dkLen=32 只需一个块，与 WebCrypto deriveBits(256) 等值。 */
function pbkdf2HmacSha256(password: Uint8Array, salt: Uint8Array): Uint8Array {
  const firstInput = new Uint8Array(salt.length + 4);
  firstInput.set(salt);
  firstInput.set([0, 0, 0, 1], salt.length); // INT(1)，大端。
  const output = new Uint8Array(32);
  let block = hmacSha256(password, firstInput);
  output.set(block);
  for (let iteration = 1; iteration < PBKDF2_ITERATIONS; iteration += 1) {
    block = hmacSha256(password, block);
    for (let index = 0; index < 32; index += 1) {
      output[index] ^= block[index];
    }
  }
  return output;
}

export async function passwordClientHash(username: string, password: string): Promise<string> {
  const encoder = new TextEncoder();
  const salt = encoder.encode(await sha256Hex(username.trim().toLowerCase()));
  const passwordBytes = encoder.encode(password);
  const subtle = globalThis.crypto?.subtle;
  if (subtle !== undefined) {
    const key = await subtle.importKey("raw", passwordBytes, "PBKDF2", false, ["deriveBits"]);
    const bits = await subtle.deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS },
      key,
      PBKDF2_BITS,
    );
    return toHex(bits);
  }
  return toHex(pbkdf2HmacSha256(passwordBytes, salt));
}
