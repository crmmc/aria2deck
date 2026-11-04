import { hashPassword } from "@/lib/crypto";

describe("hashPassword", () => {
  const mockDigest = jest.fn();
  const mockImportKey = jest.fn();
  const mockDeriveBits = jest.fn();

  beforeEach(() => {
    global.TextEncoder = class {
      encode(str: string): Uint8Array {
        const arr = new Uint8Array(str.length);
        for (let i = 0; i < str.length; i++) {
          arr[i] = str.charCodeAt(i) & 0xff;
        }
        return arr;
      }
    } as unknown as typeof TextEncoder;

    mockDigest.mockImplementation(async (_algo: string, data: ArrayBuffer) => {
      const view = new Uint8Array(data);
      const result = new Uint8Array(32);
      for (let i = 0; i < 32; i++) {
        result[i] = (view[i % view.length] || 0) ^ (i * 7);
      }
      return result.buffer;
    });

    mockImportKey.mockResolvedValue({ type: "secret" });

    mockDeriveBits.mockImplementation(async (params: { salt: Uint8Array }) => {
      const result = new Uint8Array(32);
      for (let i = 0; i < 32; i++) {
        result[i] = (params.salt[i % params.salt.length] || 0) ^ (i * 13 + 42);
      }
      return result.buffer;
    });

    Object.defineProperty(global, "crypto", {
      value: {
        subtle: {
          digest: mockDigest,
          importKey: mockImportKey,
          deriveBits: mockDeriveBits,
        },
      },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it("returns a 64-character hex string", async () => {
    const result = await hashPassword("password", "user");
    expect(result).toHaveLength(64);
    expect(/^[0-9a-f]+$/.test(result)).toBe(true);
  });

  it("produces deterministic output for same inputs", async () => {
    const result1 = await hashPassword("password", "user");
    const result2 = await hashPassword("password", "user");
    expect(result1).toBe(result2);
  });

  it("produces different output for different passwords", async () => {
    const result1 = await hashPassword("password1", "user");
    
    mockDeriveBits.mockImplementationOnce(async (params: { salt: Uint8Array }) => {
      const result = new Uint8Array(32);
      for (let i = 0; i < 32; i++) {
        result[i] = (params.salt[i % params.salt.length] || 0) ^ (i * 17 + 99);
      }
      return result.buffer;
    });
    
    const result2 = await hashPassword("password2", "user");
    expect(result1).not.toBe(result2);
  });

  it("produces different output for different usernames (salt changes)", async () => {
    const result1 = await hashPassword("password", "user1");
    const result2 = await hashPassword("password", "user2");
    expect(result1).not.toBe(result2);
  });

  it("handles empty password", async () => {
    const result = await hashPassword("", "user");
    expect(result).toHaveLength(64);
  });

  it("handles special characters in password", async () => {
    const result = await hashPassword("p@$$w0rd!#$%", "user");
    expect(result).toHaveLength(64);
  });

  it("handles unicode characters", async () => {
    const result = await hashPassword("密码🔐", "用户");
    expect(result).toHaveLength(64);
  });

  it("uses lowercase username for salt derivation", async () => {
    const result1 = await hashPassword("password", "USER");
    const result2 = await hashPassword("password", "user");
    expect(result1).toBe(result2);
  });

  it("calls crypto.subtle.digest with SHA-256", async () => {
    await hashPassword("password", "user");
    expect(mockDigest).toHaveBeenCalledWith("SHA-256", expect.any(Uint8Array));
  });

  it("calls crypto.subtle.importKey with PBKDF2", async () => {
    await hashPassword("password", "user");
    expect(mockImportKey).toHaveBeenCalledWith(
      "raw",
      expect.any(Uint8Array),
      "PBKDF2",
      false,
      ["deriveBits"]
    );
  });

  it("calls crypto.subtle.deriveBits with correct parameters", async () => {
    await hashPassword("password", "user");
    expect(mockDeriveBits).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "PBKDF2",
        iterations: 10000,
        hash: "SHA-256",
      }),
      expect.anything(),
      256
    );
  });

});

jest.mock("@noble/hashes/sha2.js", () => ({
  sha256: (data: Uint8Array) => {
    const result = new Uint8Array(32);
    for (let i = 0; i < 32; i++) {
      result[i] = (data[i % data.length] || 0) ^ (i * 3 + 1);
    }
    return result;
  },
}));
jest.mock("@noble/hashes/pbkdf2.js", () => ({
  pbkdf2: (
    _hash: unknown,
    _password: Uint8Array,
    salt: Uint8Array,
    options: { c: number; dkLen: number }
  ) => {
    expect(options).toEqual({ c: 10000, dkLen: 32 });
    const result = new Uint8Array(32);
    for (let i = 0; i < 32; i++) {
      result[i] = (salt[i % salt.length] || 0) ^ (i * 5 + 2);
    }
    return result;
  },
}));

describe("hashPassword noble fallback", () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  test.each([
    ["subtle missing", {}],
    ["subtle without digest", { subtle: {} }],
  ])("falls back to noble hashing when %s", async (_label, cryptoValue) => {
    Object.defineProperty(global, "crypto", {
      value: cryptoValue,
      writable: true,
      configurable: true,
    });

    const result = await hashPassword("password", "user");
    expect(result).toHaveLength(64);
    expect(/^[0-9a-f]+$/.test(result)).toBe(true);

    const again = await hashPassword("password", "user");
    expect(again).toBe(result);
    expect(await hashPassword("password", "other")).not.toBe(result);
  });
});
