import type { MachineStatsResponse } from "@/shared/api/types";
import { formatBytes } from "@/shared/lib/format";

/** 归档参考实现原文口径：total 无效时记 0，不做 100% 上限裁剪。 */
function toPercent(value: number, total: number): number {
  if (!total || total <= 0) return 0;
  return (value / total) * 100;
}

type MachineDiskCardProps = {
  stats: MachineStatsResponse;
};

/**
 * 归档参考「机器磁盘空间」卡片原文。数据来自 V2 `/api/v2/stats/machine` 的 disk 双段字段
 * （download_used_bytes / system_used_bytes），只在此处做一次 wire → 展示映射。
 */
export function MachineDiskCard({ stats }: MachineDiskCardProps) {
  const downloadPercent = toPercent(stats.download_used_bytes, stats.total_bytes);
  const systemPercent = toPercent(stats.system_used_bytes, stats.total_bytes);
  return (
    <div className="card mb-6">
      <h2 className="mb-4">机器磁盘空间</h2>
      <div className="flex items-baseline gap-2">
        <span className="stats-value">{formatBytes(stats.free_bytes)}</span>
        <span className="stats-unit">/ {formatBytes(stats.total_bytes)}</span>
        <span className="muted">可用</span>
      </div>
      <div className="progress-container mt-2 max-w-600" style={{ overflow: "hidden" }}>
        <div style={{ display: "flex", width: "100%", height: "100%" }}>
          <div style={{ width: `${downloadPercent}%`, background: "#3b82f6" }} />
          <div style={{ width: `${systemPercent}%`, background: "#f59e0b" }} />
        </div>
      </div>
      <div className="mt-3 text-sm" style={{ display: "grid", gap: 6 }}>
        <div className="flex items-center gap-2">
          <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#3b82f6" }} />
          <span>
            下载占用：{formatBytes(stats.download_used_bytes)}（{downloadPercent.toFixed(1)}%）
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#f59e0b" }} />
          <span>
            系统占用：{formatBytes(stats.system_used_bytes)}（{systemPercent.toFixed(1)}%）
          </span>
        </div>
        <div className="muted">
          总占用：{formatBytes(stats.used_bytes)}（
          {toPercent(stats.used_bytes, stats.total_bytes).toFixed(1)}%）
        </div>
      </div>
    </div>
  );
}
