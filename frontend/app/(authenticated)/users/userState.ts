const quotaUnitMultiplier: Record<string, number> = {
  KB: 1024,
  MB: 1024 * 1024,
  GB: 1024 * 1024 * 1024,
};

export function parseQuotaBytes(value: string, unit: string): number | null {
  const multiplier = quotaUnitMultiplier[unit];
  if (!multiplier) return null;
  const bytes = Number.parseFloat(value) * multiplier;
  return Number.isFinite(bytes) && bytes > 0 ? bytes : null;
}

export function formatQuotaForForm(bytes: number): { quotaValue: string; quotaUnit: string } {
  if (bytes >= 1024 * 1024 * 1024) {
    return { quotaValue: (bytes / (1024 * 1024 * 1024)).toFixed(2), quotaUnit: "GB" };
  }
  if (bytes >= 1024 * 1024) {
    return { quotaValue: (bytes / (1024 * 1024)).toFixed(2), quotaUnit: "MB" };
  }
  return { quotaValue: (bytes / 1024).toFixed(2), quotaUnit: "KB" };
}
