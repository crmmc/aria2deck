import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatSpeed,
} from "@/shared/lib/format";

describe("format", () => {
  it("formats bytes with units (V1 口径)", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023.0 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
    expect(formatBytes(null)).toBe("0 B");
    expect(formatBytes(undefined)).toBe("0 B");
  });

  it("formats speed with /s suffix", () => {
    expect(formatSpeed(2048)).toBe("2.0 KB/s");
  });

  it("formats datetime with padding", () => {
    const ms = new Date(2026, 0, 5, 3, 4, 5).getTime();
    expect(formatDateTime(ms)).toBe("2026-01-05 03:04:05");
    expect(formatDateTime(null)).toBe("—");
  });

  it("formats duration", () => {
    expect(formatDuration(65000)).toBe("1:05");
    expect(formatDuration(3600000 + 65000)).toBe("1:01:05");
    expect(formatDuration(-1)).toBe("—");
  });

  it("formats percent", () => {
    expect(formatPercent(50, 100)).toBe("50%");
    expect(formatPercent(150, 100)).toBe("100%");
    expect(formatPercent(10, null)).toBe("—");
    expect(formatPercent(10, 0)).toBe("—");
  });
});
