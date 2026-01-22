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
    // Initial fetch
    api.getStats().then(setStats).catch(console.error);

    // Poll every 5 seconds
    const interval = setInterval(() => {
      api.getStats().then(setStats).catch(console.error);
    }, 5000);

    return () => clearInterval(interval);
  }, []);

  if (!stats) return null;

  const diskPercent = (stats.disk_used_space / stats.disk_total_space) * 100;

  // Determine disk usage color based on percentage
  const getDiskColor = (percent: number) => {
    if (percent >= 80) return "#ff3b30"; // Red
    if (percent >= 50) return "#ff9500"; // Yellow/Orange
    return "#34c759"; // Green
  };

  return (
    <div className="card row" style={{ gap: 24, marginBottom: 20 }}>
      <div style={{ flex: 1 }}>
        <h3
          className="muted"
          style={{ fontSize: 13, textTransform: "uppercase" }}
        >
          空间使用
        </h3>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontSize: 24, fontWeight: 600 }}>
            {formatBytes(stats.disk_used_space)}
          </span>
          <span className="muted" style={{ fontSize: 16 }}>
            / {formatBytes(stats.disk_total_space)}
          </span>
          {stats.disk_space_limited && (
            <span
              style={{
                color: "#ff9500",
                fontSize: 16,
                cursor: "help",
                marginLeft: 4,
              }}
              title="当前机器空间受限，您的可用空间已被调整，请联系管理员"
            >
              ⚠️
            </span>
          )}
        </div>
        <div
          style={{
            height: 6,
            background: "rgba(0,0,0,0.05)",
            borderRadius: 3,
            marginTop: 8,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${diskPercent}%`,
              background: getDiskColor(diskPercent),
              transition: "width 0.5s ease, background 0.3s ease",
            }}
          />
        </div>
      </div>

      <div style={{ width: 1, height: 40, background: "rgba(0,0,0,0.1)" }} />

      <div style={{ flex: 1 }}>
        <h3
          className="muted"
          style={{ fontSize: 13, textTransform: "uppercase" }}
        >
          任务速度
        </h3>
        <div className="row" style={{ gap: 20 }}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#34c759" }}>
              ↓ {formatBytes(stats.download_speed)}/s
            </div>
          </div>
          <div>
            <div style={{ fontSize: 18, fontWeight: 600, color: "#0071e3" }}>
              ↑ {formatBytes(stats.upload_speed)}/s
            </div>
          </div>
        </div>
      </div>

      <div style={{ width: 1, height: 40, background: "rgba(0,0,0,0.1)" }} />

      <div style={{ flex: 0.5 }}>
        <h3
          className="muted"
          style={{ fontSize: 13, textTransform: "uppercase" }}
        >
          活跃任务
        </h3>
        <div style={{ fontSize: 24, fontWeight: 600 }}>
          {stats.active_task_count}
        </div>
      </div>
    </div>
  );
}
