import { copyText } from "@/shared/lib/clipboard";

describe("copyText（T12 剪贴板降级）", () => {
  const originalClipboard = navigator.clipboard;

  afterEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: originalClipboard,
      configurable: true,
    });
  });

  it("uses navigator.clipboard when available", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    await expect(copyText("https://example.com/a")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("https://example.com/a");
  });

  it("falls back to execCommand when clipboard is missing", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const execCommand = jest.fn().mockReturnValue(true);
    document.execCommand = execCommand as typeof document.execCommand;
    try {
      await expect(copyText("magnet:?xt=1")).resolves.toBe(true);
      expect(execCommand).toHaveBeenCalledWith("copy");
    } finally {
      delete (document as { execCommand?: unknown }).execCommand;
    }
  });

  it("falls back to execCommand when clipboard rejects", async () => {
    const writeText = jest.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const execCommand = jest.fn().mockReturnValue(true);
    document.execCommand = execCommand as typeof document.execCommand;
    try {
      await expect(copyText("https://b")).resolves.toBe(true);
      expect(execCommand).toHaveBeenCalledWith("copy");
    } finally {
      delete (document as { execCommand?: unknown }).execCommand;
    }
  });

  it("returns false when both paths fail", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const execCommand = jest.fn().mockReturnValue(false);
    document.execCommand = execCommand as typeof document.execCommand;
    try {
      await expect(copyText("https://c")).resolves.toBe(false);
    } finally {
      delete (document as { execCommand?: unknown }).execCommand;
    }
  });
});
