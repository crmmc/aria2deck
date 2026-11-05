import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatSpeed,
} from "@/shared/lib/format";

describe("format", () => {
  it("formats bytes with units", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1.00 KiB");
    expect(formatBytes(1536)).toBe("1.50 KiB");
    expect(formatBytes(1024 * 1024)).toBe("1.00 MiB");
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
  });

  it("formats speed with /s suffix", () => {
    expect(formatSpeed(2048)).toBe("2.00 KiB/s");
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
