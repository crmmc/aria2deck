"use client";

import { useEffect, useState } from "react";

import { api } from "@/shared/api/client";
import { formatBytes } from "@/shared/lib/format";
import type { TaskStatsResponse } from "@/shared/api/types";

function toPercent(used: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0;
  const value = (used / total) * 100;
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function getDiskColor(percent: number): string {
  if (percent >= 80) return "var(--danger)";
  if (percent >= 50) return "var(--warning)";
  return "var(--success)";
}

/** C26：归档参考 StatsWidget 用机器磁盘口径（disk），不用 quota。 */
export function StatsWidget() {
  const [stats, setStats] = useState<TaskStatsResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    let isFetching = false;
    const load = () => {
      api
        .get<TaskStatsResponse>("/api/v2/tasks/stats")
        .then((s) => {
          if (!cancelled) setStats(s);
        })
        .catch(() => {
          /* 保持上次读数 */
        });
    };
    load();
    const interval = setInterval(() => {
      if (document.hidden || isFetching) return;
      isFetching = true;
      api
        .get<TaskStatsResponse>("/api/v2/tasks/stats")
        .then((s) => {
          if (!cancelled) setStats(s);
        })
        .catch(() => {
          /* 保持上次读数 */
        })
        .finally(() => {
          isFetching = false;
        });
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (!stats) return null;

  const disk = stats.disk;
  const diskPercent = toPercent(disk.used_bytes, disk.total_bytes);
  const frozenSpace = disk.reserved_bytes;

  return (
    <div className="card row stats-widget">
      <div className="stats-section">
        <h3 className="stats-label">可用空间</h3>
        <div className="flex items-baseline gap-2">
          <span className="stats-value">
            {formatBytes(disk.total_bytes - disk.used_bytes - frozenSpace)}
          </span>
          <span className="stats-unit">/ {formatBytes(disk.total_bytes)}</span>
          {disk.limited && (
            <span
              className="space-warning"
              title="当前机器可分配空间已被其他任务占用，可用空间已按全局预算调整"
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
        <div className="stats-value">{stats.counts.active}</div>
      </div>
    </div>
  );
}
