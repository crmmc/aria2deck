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

  it("falls back to pure-js hashing when SubtleCrypto is unavailable", async () => {
    Object.defineProperty(global, "crypto", {
      value: {},
      writable: true,
      configurable: true,
    });

    const result = await hashPassword("password", "user");

    expect(result).toBe("5a63524297fbbf5df0f2f10ff13fba9a19168b9d7a3a4e76fddc81e12f46b2f1");
    expect(mockDigest).not.toHaveBeenCalled();
    expect(mockImportKey).not.toHaveBeenCalled();
    expect(mockDeriveBits).not.toHaveBeenCalled();
  });
});
