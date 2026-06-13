"use client";

import { useEffect, useState } from "react";
import { SystemStats } from "@/types";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";

export default function StatsWidget() {
  const [stats, setStats] = useState<SystemStats | null>(null);

  const toPercent = (used: number, total: number): number => {
    if (!Number.isFinite(total) || total <= 0) return 0;
    const value = (used / total) * 100;
    if (!Number.isFinite(value)) return 0;
    return Math.max(0, Math.min(100, value));
  };

  useEffect(() => {
    let cancelled = false;
    api.getStats().then((s) => { if (!cancelled) setStats(s); }).catch(console.error);
    const interval = setInterval(() => {
      api.getStats().then((s) => { if (!cancelled) setStats(s); }).catch(console.error);
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!stats) return null;

  const diskPercent = toPercent(stats.disk_used_space, stats.disk_total_space);
  const frozenSpace = stats.disk_frozen_space || 0;

  const getDiskColor = (percent: number) => {
    if (percent >= 80) return "var(--danger)";
    if (percent >= 50) return "var(--warning)";
    return "var(--success)";
  };

  return (
    <div className="card row stats-widget">
      <div className="stats-section">
        <h3 className="stats-label">可用空间</h3>
        <div className="flex items-baseline gap-2">
          <span className="stats-value">{formatBytes(stats.disk_total_space - stats.disk_used_space - frozenSpace)}</span>
          <span className="stats-unit">/ {formatBytes(stats.disk_total_space)}</span>
          {stats.disk_space_limited && (
            <span
              className="space-warning"
              title="当前机器空间受限，您的可用空间已被调整，请联系管理员"
            >
              ⚠️
            </span>
          )}
        </div>
        <div className="progress-container mt-2">
          <div
            className="progress-bar"
            style={{
              width: `${diskPercent}%`,
              background: getDiskColor(diskPercent),
            }}
          />
        </div>
        {frozenSpace > 0 && (
          <div className="text-xs mt-1" style={{ color: "var(--text-tertiary)" }}>
            已冻结: {formatBytes(frozenSpace)} (下载中)
          </div>
        )}
      </div>

      <div className="divider-v" />

      <div className="stats-section">
        <h3 className="stats-label">任务速度</h3>
        <div className="row gap-5">
          <div>
            <div className="text-lg font-semibold speed-download">
              ↓ {formatBytes(stats.download_speed)}/s
            </div>
          </div>
          <div>
            <div className="text-lg font-semibold speed-upload">
              ↑ {formatBytes(stats.upload_speed)}/s
            </div>
          </div>
        </div>
      </div>

      <div className="divider-v" />

      <div className="stats-section-half">
        <h3 className="stats-label">活跃任务</h3>
        <div className="stats-value">{stats.active_task_count}</div>
      </div>
    </div>
  );
}
