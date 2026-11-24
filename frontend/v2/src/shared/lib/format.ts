/** 字节格式化（归档参考口径：B/KB/MB/GB/TB，固定 1 位小数）。 */

export function formatBytes(value?: number | null): string {
  if (value === 0 || value == null || Number.isNaN(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

/** 管理员页面：大于等于 100 的换算值和字节整数不保留小数。 */
export function formatAdminBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unitIndex = 0;
  let converted = value;
  while (converted >= 1024 && unitIndex < units.length - 1) {
    converted /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 || converted >= 100 ? 0 : 1;
  return `${converted.toFixed(digits)} ${units[unitIndex]}`;
}

export function formatAdminDateTime(value: number): string {
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}
