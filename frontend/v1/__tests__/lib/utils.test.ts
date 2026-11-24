import { bytesToGB, gbToBytes, formatBytes } from "@/lib/utils";

describe("bytesToGB", () => {
  it("converts 0 bytes to 0.00", () => {
    expect(bytesToGB(0)).toBe("0.00");
  });

  it("converts 1GB to 1.00", () => {
    expect(bytesToGB(1073741824)).toBe("1.00");
  });

  it("converts 2.5GB correctly", () => {
    expect(bytesToGB(2684354560)).toBe("2.50");
  });

  it("converts TB range values", () => {
    expect(bytesToGB(1099511627776)).toBe("1024.00");
  });

  it("handles fractional GB", () => {
    expect(bytesToGB(536870912)).toBe("0.50");
  });
});

describe("gbToBytes", () => {
  it("converts 0 GB to 0 bytes", () => {
    expect(gbToBytes(0)).toBe(0);
  });

  it("converts 1 GB to correct bytes", () => {
    expect(gbToBytes(1)).toBe(1073741824);
  });

  it("converts 0.5 GB to correct bytes", () => {
    expect(gbToBytes(0.5)).toBe(536870912);
  });

  it("returns integer (rounds)", () => {
    const result = gbToBytes(0.123456789);
    expect(Number.isInteger(result)).toBe(true);
  });

  it("handles large values", () => {
    expect(gbToBytes(100)).toBe(107374182400);
  });
});

describe("formatBytes", () => {
  it("formats 0 as 0 B", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  it("formats null as 0 B", () => {
    expect(formatBytes(null)).toBe("0 B");
  });

  it("formats undefined as 0 B", () => {
    expect(formatBytes(undefined)).toBe("0 B");
  });

  it("formats NaN as 0 B", () => {
    expect(formatBytes(NaN)).toBe("0 B");
  });

  it("formats bytes correctly", () => {
    expect(formatBytes(500)).toBe("500.0 B");
    expect(formatBytes(1023)).toBe("1023.0 B");
  });

  it("formats KB correctly", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1025)).toBe("1.0 KB");
    expect(formatBytes(2048)).toBe("2.0 KB");
  });

  it("formats MB correctly", () => {
    expect(formatBytes(1048576)).toBe("1.0 MB");
    expect(formatBytes(5242880)).toBe("5.0 MB");
  });

  it("formats GB correctly", () => {
    expect(formatBytes(1073741824)).toBe("1.0 GB");
    expect(formatBytes(10737418240)).toBe("10.0 GB");
  });

  it("formats TB correctly", () => {
    expect(formatBytes(1099511627776)).toBe("1.0 TB");
  });

  it("handles edge case at unit boundaries", () => {
    expect(formatBytes(1023)).toBe("1023.0 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1048575)).toBe("1024.0 KB");
    expect(formatBytes(1048576)).toBe("1.0 MB");
  });
});
