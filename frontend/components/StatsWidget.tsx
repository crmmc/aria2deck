"use client";

import { useEffect, useState } from "react";
import { SystemStats } from "@/types";
import { api } from "@/lib/api";

function formatBytes(value?: number | null) {
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

export default function StatsWidget() {
  const [stats, setStats] = useState<SystemStats | null>(null);

  useEffect(() => {
    api.getStats().then(setStats).catch(console.error);
    const interval = setInterval(() => {
      api.getStats().then(setStats).catch(console.error);
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  if (!stats) return null;

  const diskPercent = (stats.disk_used_space / stats.disk_total_space) * 100;

  const getDiskColor = (percent: number) => {
    if (percent >= 80) return "var(--danger)";
    if (percent >= 50) return "var(--warning)";
    return "var(--success)";
  };

  return (
    <div className="card row stats-widget">
      <div className="stats-section">
        <h3 className="stats-label">空间使用</h3>
        <div className="flex items-baseline gap-2">
          <span className="stats-value">{formatBytes(stats.disk_used_space)}</span>
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
