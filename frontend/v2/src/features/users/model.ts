import type { UserResponse } from "@/shared/api/types";

/** 归档参考 用户视图：wire 字段 + 由全量列表按 归档参考 原公式计算的占比（C17）。 */
export interface UserView extends UserResponse {
  usage_percent: number;
  machine_share_percent: number;
}

/** 归档参考 原公式（backend 归档/app/domain/quota.py:113-124）：百分比保留 1 位并夹在 0..100。 */
export function usagePercent(usedBytes: number, quotaBytes: number): number {
  if (quotaBytes <= 0) return 0;
  return round1(Math.min(100, Math.max(0, (usedBytes * 100) / quotaBytes)));
}

export function machineSharePercent(usedBytes: number, totalUsedBytes: number): number {
  if (totalUsedBytes <= 0) return 0;
  return round1(Math.min(100, Math.max(0, (usedBytes * 100) / totalUsedBytes)));
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

/** 全量用户 → 归档参考 视图：机器占比的分母是全部用户 used 之和（归档参考 list_users 同口径）。 */
export function toUserViews(users: UserResponse[]): UserView[] {
  const totalUsedBytes = users.reduce((sum, user) => sum + user.used_bytes, 0);
  return users.map((user) => ({
    ...user,
    usage_percent: usagePercent(user.used_bytes, user.quota_bytes),
    machine_share_percent: machineSharePercent(user.used_bytes, totalUsedBytes),
  }));
}

/** 归档参考 UsersTable 原文阈值：≥90% 危险，≥70% 警告，其余成功。 */
export function usageColor(percent: number): string {
  if (percent >= 90) return "var(--danger)";
  if (percent >= 70) return "var(--warning)";
  return "var(--success)";
}

/** 进度条分段宽度（归档参考 UsersTable 原文）：已用与冻结各自占配额的比例，合计不超过 100%。 */
export function storageBarSegments(user: UserResponse): { usedPct: number; reservedPct: number } {
  if (user.quota_bytes <= 0) return { usedPct: 0, reservedPct: 0 };
  const usedPct = Math.min(100, (user.used_bytes / user.quota_bytes) * 100);
  const reservedPct = Math.min(100 - usedPct, (user.reserved_bytes / user.quota_bytes) * 100);
  return { usedPct, reservedPct };
}
