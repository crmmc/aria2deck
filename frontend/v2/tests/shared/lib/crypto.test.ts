import { sha256Hex } from "@/shared/lib/crypto";

const ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

describe("sha256Hex", () => {
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
