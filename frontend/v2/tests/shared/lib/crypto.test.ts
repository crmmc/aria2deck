import { passwordClientHash, sha256Hex } from "@/shared/lib/crypto";

const ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");

function defineCrypto(value: unknown): void {
  Object.defineProperty(globalThis, "crypto", {
    value,
    configurable: true,
    writable: true,
  });
}

afterEach(() => {
  if (cryptoDescriptor !== undefined) {
    Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
  } else {
    delete (globalThis as { crypto?: unknown }).crypto;
  }
});

describe("sha256Hex", () => {
  it("pure JS fallback (no subtle) produces the known SHA-256 vector", async () => {
    defineCrypto({}); // 非安全上下文：crypto 存在但无 subtle。
    await expect(sha256Hex("abc")).resolves.toBe(ABC_SHA256);
    await expect(sha256Hex("")).resolves.toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });

  it("WebCrypto subtle path produces the same digest as the known vector", async () => {
    const { webcrypto } = await import("node:crypto");
    defineCrypto(webcrypto);
    await expect(sha256Hex("abc")).resolves.toBe(ABC_SHA256);
  });

  it("pure JS and subtle paths agree on a multi-block input", async () => {
    const long = "aria2deck-".repeat(60); // 600 字节，跨多个 64 字节分组。
    const { webcrypto } = await import("node:crypto");
    defineCrypto(webcrypto);
    const subtleResult = await sha256Hex(long);
    defineCrypto({});
    expect(subtleResult).toMatch(/^[0-9a-f]{64}$/);
    await expect(sha256Hex(long)).resolves.toBe(subtleResult);
  });

  it("uses the available subtle digest when present", async () => {
    const digest = jest.fn(async () => new Uint8Array([0xab, 0xcd]).buffer);
    defineCrypto({ subtle: { digest } });
    await expect(sha256Hex("secret")).resolves.toBe("abcd");
    expect(digest).toHaveBeenCalledTimes(1);
  });
});

describe("passwordClientHash", () => {
  // 独立基准向量（python）：
  // hashlib.pbkdf2_hmac('sha256', password.encode(),
  //                     hashlib.sha256(username.strip().lower().encode()).hexdigest().encode(), 10000).hex()
  // 覆盖：用户名大小写/空白规范化、非 ASCII 密码、超长用户名（盐跨分组）、密码 >64 字节（HMAC 密钥再哈希分支）。
  const VECTORS: ReadonlyArray<readonly [string, string, string]> = [
    ["admin", "right-password", "da5b623564816f0bbdf7a25143f9a91d7d302c88dd976e3d33c2f64b9a0c7a4c"],
    ["Admin", "vector-password", "50c3e6341f6f53d54582ea8c9fc20cad7d3bc7b8aaeccfff5692d903143d5d62"],
    ["  Admin  ", "vector-password", "50c3e6341f6f53d54582ea8c9fc20cad7d3bc7b8aaeccfff5692d903143d5d62"],
    ["vector-user", "ה密码-with-unicode-パス", "0019a8113565d58a8fe4fa0878f3e9e16ea26c94a72419eb7ba0611400872bd3"],
    ["u".repeat(70), "long-username-over-64", "0e9987d728eb8f2ea17f969f93b7ed71dc07f473473eb80dcb1e48fc17da5622"],
    ["len-65-byte-password-check", "p".repeat(65), "73b19f4d2a60ae8ccf9d104ee87eba7449ecabf2a969c04d96a72c7df0da4bcd"],
  ];

  it.each(VECTORS)("pure JS fallback matches the known vector (%s)", async (username, password, expected) => {
    defineCrypto({});
    await expect(passwordClientHash(username, password)).resolves.toBe(expected);
  });

  it.each(VECTORS)("WebCrypto path matches the same vector (%s)", async (username, password, expected) => {
    const { webcrypto } = await import("node:crypto");
    defineCrypto(webcrypto);
    await expect(passwordClientHash(username, password)).resolves.toBe(expected);
  });

});
