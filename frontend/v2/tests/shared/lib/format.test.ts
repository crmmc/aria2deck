import {
  formatAdminBytes,
  formatAdminDateTime,
  formatBytes,
} from "@/shared/lib/format";

describe("format", () => {
  it("formats bytes with units (归档参考口径)", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023.0 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
    expect(formatBytes(null)).toBe("0 B");
    expect(formatBytes(undefined)).toBe("0 B");
  });
});

describe("formatAdminBytes", () => {
  it.each([
    [1023, "1023 B"],
    [1024, "1.0 KB"],
    [99.5 * 1024, "99.5 KB"],
    [100 * 1024, "100 KB"],
    [5 * 1024 ** 3, "5.0 GB"],
    [1024 ** 5, "1024 TB"],
  ])("formats %s with automatic precision", (value, expected) => {
    expect(formatAdminBytes(value)).toBe(expected);
  });

  it.each([0, -1, null, undefined, Number.NaN, Number.POSITIVE_INFINITY])(
    "formats non-positive or invalid input %s as zero bytes",
    (value) => {
      expect(formatAdminBytes(value)).toBe("0 B");
    },
  );
});

describe("formatAdminDateTime", () => {
  it("uses zh-CN local date and minute precision", () => {
    expect(formatAdminDateTime(new Date(2026, 10, 23, 9, 5, 59).getTime())).toBe(
      "2026/11/23 09:05",
    );
    expect(formatAdminDateTime(new Date(2026, 0, 2, 0, 5, 59).getTime())).toBe(
      "2026/01/02 00:05",
    );
  });
});
