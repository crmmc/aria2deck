"use client";

import { useEffect, useState } from "react";

import { api } from "@/shared/api/client";
import { formatBytes } from "@/shared/lib/format";
import { Icon } from "@/shared/components/Icon";
import type { TaskStatsResponse } from "@/shared/api/types";

const SAMPLE_WINDOW_MS = 60_000;
const MAX_SAMPLES = 13;
// 有任务在跑时加快刷新，否则短任务的冻结量会落在采样间隙里看不见
const ACTIVE_POLL_MS = 2_000;
const IDLE_POLL_MS = 5_000;
// 与 svg 的 viewBox 一致：端点标记用百分比定位到这一坐标系
const VIEW_WIDTH = 640;
const VIEW_HEIGHT = 160;

type SpeedSample = {
  at: number;
  download: number;
  upload: number;
};

type ChartPoint = { x: number; y: number };

function toPercent(used: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0;
  const value = (used / total) * 100;
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function linePath(points: ChartPoint[]): string {
  if (points.length < 2) return "";
  return points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
}

function SpeedChart({ samples }: { samples: SpeedSample[] }) {
  const endAt = samples.at(-1)?.at ?? 0;
  const peak = Math.max(...samples.flatMap((sample) => [sample.download, sample.upload]), 1);
  const toPoints = (key: "download" | "upload"): ChartPoint[] => samples.map((sample) => {
    const position = Math.max(0, Math.min(1, 1 - (endAt - sample.at) / SAMPLE_WINDOW_MS));
    return {
      x: 10 + position * 620,
      y: 146 - (sample[key] / (peak * 1.12)) * 116,
    };
  });
  const downloadPoints = toPoints("download");
  const uploadPoints = toPoints("upload");
  const downloadLine = linePath(downloadPoints);
  const lastDownload = downloadPoints.at(-1);
  const lastUpload = uploadPoints.at(-1);

  return (
    <div className="speed-chart-wrap">
      {/* svg 用 preserveAspectRatio="none" 铺满卡片，内部圆点会被拽成椭圆，
          所以端点标记放在 svg 之外的等比层里。 */}
      <div className="speed-chart-plot">
        <svg
          className="speed-chart"
          viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`最近 60 秒下载和上传速度，${samples.length} 次真实采样`}
        >
          <defs>
            <linearGradient id="speed-area-gradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#0071e3" stopOpacity=".28" />
              <stop offset="100%" stopColor="#64c8ff" stopOpacity=".015" />
            </linearGradient>
          </defs>
          <path className="chart-grid" d="M10 34H630M10 72H630M10 110H630M10 146H630" />
          {downloadLine && <path className="speed-area" d={`${downloadLine} L${lastDownload!.x.toFixed(1)} 146 L${downloadPoints[0].x.toFixed(1)} 146 Z`} />}
          {downloadLine && <path className="speed-line" d={downloadLine} />}
          {uploadPoints.length > 1 && <path className="upload-line" d={linePath(uploadPoints)} />}
        </svg>
        {lastDownload && (
          <span
            className="speed-point"
            aria-hidden="true"
            style={{
              left: `${(lastDownload.x / VIEW_WIDTH) * 100}%`,
              top: `${(lastDownload.y / VIEW_HEIGHT) * 100}%`,
            }}
          />
        )}
        {lastUpload && (
          <span
            className="upload-point"
            aria-hidden="true"
            style={{
              left: `${(lastUpload.x / VIEW_WIDTH) * 100}%`,
              top: `${(lastUpload.y / VIEW_HEIGHT) * 100}%`,
            }}
          />
        )}
      </div>
      {samples.length < 2 && (
        <div className="chart-sample-state" role="status">
          {samples.length === 0 ? "等待真实速度样本" : "仅 1 次真实采样，等待下一次连线"}
        </div>
      )}
      <div className="chart-axis">
        <span>{samples.length > 1 ? "60 秒前" : "真实采样"}</span>
        <span>现在</span>
      </div>
    </div>
  );
}

/** C26：归档参考 StatsWidget 用机器磁盘口径（disk），不用 quota。 */
export function StatsWidget() {
  const [stats, setStats] = useState<TaskStatsResponse | null>(null);
  const [samples, setSamples] = useState<SpeedSample[]>([]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let nextDelay = IDLE_POLL_MS;
    const load = () => {
      if (document.hidden) {
        timer = setTimeout(load, IDLE_POLL_MS);
        return;
      }
      api
        .get<TaskStatsResponse>("/api/v2/tasks/stats")
        .then((nextStats) => {
          if (cancelled) return;
          setStats(nextStats);
          nextDelay = nextStats.counts.active > 0 || nextStats.counts.queued > 0
            ? ACTIVE_POLL_MS
            : IDLE_POLL_MS;
          const sample: SpeedSample = {
            at: Date.now(),
            download: nextStats.download_speed,
            upload: nextStats.upload_speed,
          };
          setSamples((previous) => [...previous, sample]
            .filter((item) => sample.at - item.at <= SAMPLE_WINDOW_MS)
            .slice(-MAX_SAMPLES));
        })
        .catch(() => {
          /* 保持上次读数 */
        })
        .finally(() => {
          if (cancelled) return;
          timer = setTimeout(load, nextDelay);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const quota = stats?.quota;
  const disk = stats?.disk;
  // 用户视角的「已使用」含在途冻结，与「可用」共用同一口径：total = 已用 + 冻结 + 可用。
  const usedTotal = disk ? disk.used_bytes + disk.reserved_bytes : 0;
  const usedPercent = disk ? toPercent(usedTotal, disk.total_bytes) : 0;
  const storedPercent = disk ? toPercent(disk.used_bytes, disk.total_bytes) : 0;
  const reservedPercent = disk ? toPercent(disk.reserved_bytes, disk.total_bytes) : 0;
  const available = disk ? disk.total_bytes - usedTotal : null;

  return (
    <section className="card transmission-card" aria-label="实时传输">
      <div className="transmission-main">
        <div className="transmission-heading">
          <div className="speed-heading speed-download-group">
            <span className="section-kicker">下载速度</span>
            <h2><Icon name="download" className="icon-sm speed-download" /><span>{stats ? formatBytes(stats.download_speed) : "—"}</span><small>/s</small></h2>
          </div>
          <div className="speed-heading speed-upload-group">
            <span className="section-kicker">上传速度</span>
            <h2><Icon name="upload" className="icon-sm speed-upload" /><span>{stats ? formatBytes(stats.upload_speed) : "—"}</span><small>/s</small></h2>
          </div>
          <div className="activity-heading">
            <strong><span>{stats?.counts.active ?? "—"}</span> <small>个</small></strong>
            <span>活动任务</span>
          </div>
        </div>
        <div className="transmission-body"><SpeedChart samples={samples} /></div>
      </div>

      <aside className="storage-panel" aria-label="存储空间">
        <div className="storage-title">
          存储空间
          {disk?.limited && (
            <span
              className="space-warning"
              title="当前机器可分配空间已被其他任务占用，可用空间已按全局预算调整"
              aria-label="可用空间已按全局预算调整"
            >
              <Icon name="alert" className="icon-sm" />
            </span>
          )}
        </div>
        <div className="storage-available">
          <strong>{available === null ? "—" : formatBytes(available)}</strong>
          <span>可用</span>
        </div>
        <div
          className="storage-track progress-container"
          role="meter"
          aria-label={
            disk && disk.reserved_bytes > 0
              ? `存储空间已使用 ${usedPercent.toFixed(0)}%，其中下载中 ${formatBytes(disk.reserved_bytes)}`
              : `存储空间已使用 ${usedPercent.toFixed(0)}%`
          }
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={usedPercent}
          aria-valuetext={available === null ? undefined : `可用 ${formatBytes(available)}`}
        >
          <span className="progress-bar" style={{ width: `${storedPercent}%` }} />
          {reservedPercent > 0 && (
            <span className="progress-bar-reserved" style={{ width: `${reservedPercent}%` }} />
          )}
        </div>
        <div className="storage-detail">
          {disk ? (
            <>
              <span>已使用 {formatBytes(usedTotal)}（{usedPercent.toFixed(0)}%）</span>
              {disk.reserved_bytes > 0 && (
                <span className="storage-frozen" title="其中正在下载、尚未落盘的部分">
                  已冻结 {formatBytes(disk.reserved_bytes)}
                </span>
              )}
              <span>总空间 {formatBytes(disk.total_bytes)}</span>
              {disk.limited && quota && (
                <span
                  className="storage-limited"
                  title="机器磁盘余量不足，可用空间已按全局预算收紧"
                >
                  配额 {formatBytes(quota.quota_bytes)}
                </span>
              )}
            </>
          ) : <span>等待真实磁盘数据</span>}
        </div>
      </aside>
    </section>
  );
}
