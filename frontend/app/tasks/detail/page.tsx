"use client";

import Link from "next/link";
import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { api, taskWsUrl } from "@/lib/api";
import { useToast } from "@/components/Toast";
import SpeedChart from "@/components/SpeedChart";
import FileList from "@/components/FileList";
import type { Task, TaskFile } from "@/types";

function formatBytes(value: number) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

function TaskDetailContent() {
  const router = useRouter();
  const { showToast } = useToast();
  const searchParams = useSearchParams();
  const taskId = useMemo(() => Number(searchParams.get("id")), [searchParams]);
  const [task, setTask] = useState<Task | null>(null);
  const [taskDetail, setTaskDetail] = useState<any>(null);
  const [files, setFiles] = useState<TaskFile[]>([]);
  const [samples, setSamples] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);

  const magnetLink = useMemo(() => {
    if (!taskDetail?.aria2_detail?.info_hash) return null;
    const infoHash = taskDetail.aria2_detail.info_hash;
    const name = taskDetail.aria2_detail.bittorrent?.info?.name || task?.name;
    let magnet = `magnet:?xt=urn:btih:${infoHash}`;
    if (name) {
      magnet += `&dn=${encodeURIComponent(name)}`;
    }
    return magnet;
  }, [taskDetail, task]);

  function copyMagnetLink() {
    if (!magnetLink) return;
    navigator.clipboard
      .writeText(magnetLink)
      .then(() => {
        showToast("磁力链接已复制到剪贴板", "success");
      })
      .catch(() => {
        showToast("复制失败，请手动复制", "error");
      });
  }

  useEffect(() => {
    if (!taskId || Number.isNaN(taskId)) {
      setError("缺少任务 ID。");
      return;
    }
    api
      .me()
      .catch(() => {
        router.push("/login");
      })
      .then(() =>
        Promise.all([
          api.getTask(taskId),
          api.getTaskDetail(taskId),
          api.getTaskFiles(taskId).catch(() => []), // 忽略文件获取错误（例如任务在 aria2 中不活跃）
        ]),
      )
      .then(([taskData, detailData, filesData]) => {
        setTask(taskData);
        setTaskDetail(detailData);
        setFiles(filesData);
        setSamples([taskData.download_speed]);
      })
      .catch((err) => setError((err as Error).message));
  }, [router, taskId]);

  useEffect(() => {
    if (!taskId || Number.isNaN(taskId)) {
      return;
    }
    const ws = new WebSocket(taskWsUrl());
    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "task_update" && payload.task.id === taskId) {
        setTask(payload.task);
        setSamples((prev) => {
          const next = [...prev, payload.task.download_speed];
          return next.slice(-60);
        });

        // 如果文件列表为空则刷新（例如元数据已下载）
        if (files.length === 0 && payload.task.total_length > 0) {
          api
            .getTaskFiles(taskId)
            .then(setFiles)
            .catch(() => {});
        }
      }
    };
    const ping = setInterval(
      () => ws.readyState === 1 && ws.send("ping"),
      15000,
    );
    return () => {
      clearInterval(ping);
      ws.close();
    };
  }, [taskId, files.length]);

  if (error) {
    return <p className="muted">{error}</p>;
  }

  if (!task) {
    return <p className="muted">加载中...</p>;
  }

  return (
    <div className="glass-frame animate-in">
      <Link
        href="/tasks"
        className="muted back-link"
      >
        ← 返回任务列表
      </Link>

      <div className="card">
        <div className="space-between flex-start">
          <div className="detail-header">
            <h1
              className="detail-title"
              title={task.name || task.uri}
            >
              {task.name || "未命名任务"}
            </h1>
            <p
              className="muted truncate"
              title={task.uri}
            >
              {task.uri}
            </p>
          </div>
          <span
            className={`badge badge-lg ${task.status === "active" ? "active" : task.status === "complete" ? "complete" : ""}`}
          >
            {task.status}
          </span>
        </div>

        <div className="detail-stats-grid">
          <div>
            <p className="muted detail-stat-label">
              进度
            </p>
            <div className="detail-stat-value">
              {formatBytes(task.completed_length)}
              <span className="muted font-normal text-md">
                {" "}
                / {formatBytes(task.total_length)}
              </span>
            </div>
            <div className="muted detail-stat-sub">
              {task.total_length > 0
                ? `${((task.completed_length / task.total_length) * 100).toFixed(2)}%`
                : "0%"}
            </div>
          </div>
          <div>
            <p className="muted detail-stat-label">
              当前速度
            </p>
            <div className="detail-stat-value text-info">
              {formatBytes(task.download_speed)}/s
            </div>
            {taskDetail?.peak_download_speed > 0 && (
              <div className="muted detail-stat-sub">
                峰值: {formatBytes(taskDetail.peak_download_speed)}/s
              </div>
            )}
          </div>
          <div>
            <p className="muted detail-stat-label">
              上传速度
            </p>
            <div className="detail-stat-value text-success">
              {formatBytes(task.upload_speed)}/s
            </div>
          </div>
          {taskDetail?.aria2_detail?.connections !== undefined && (
            <div>
              <p className="muted detail-stat-label">
                连接数
              </p>
              <div className="detail-stat-value">
                {taskDetail.aria2_detail.connections}
              </div>
              {taskDetail?.peak_connections > 0 && (
                <div className="muted detail-stat-sub">
                  峰值: {taskDetail.peak_connections}
                </div>
              )}
            </div>
          )}
          {taskDetail?.aria2_detail?.num_seeders !== undefined && (
            <div>
              <p className="muted detail-stat-label">
                做种数
              </p>
              <div className="detail-stat-value">
                {taskDetail.aria2_detail.num_seeders}
              </div>
            </div>
          )}
          {taskDetail?.aria2_detail?.info_hash && (
            <div className="span-full">
              <p className="muted detail-stat-label">
                Info Hash
              </p>
              <div className="detail-info-hash">
                {taskDetail.aria2_detail.info_hash}
              </div>
              {magnetLink && (
                <button
                  type="button"
                  className="button secondary btn-sm mt-2"
                  onClick={copyMagnetLink}
                >
                  复制磁力链接
                </button>
              )}
            </div>
          )}
          {task.error ? (
            <div className="span-full error-box">
              错误：{task.error}
            </div>
          ) : null}
        </div>

        {task.artifact_token ? (
          <div className="mt-7">
            <a
              className="button btn-task text-md"
              href={`${process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}/api/tasks/artifacts/${task.artifact_token}`}
            >
              下载文件
            </a>
          </div>
        ) : null}
      </div>

      <div className="card detail-section">
        <h3 className="detail-section-title">实时速度</h3>
        <SpeedChart samples={samples} height={200} />
      </div>

      <div className="card detail-section">
        <h3 className="detail-section-title">文件列表</h3>
        <FileList files={files} />
      </div>
    </div>
  );
}

export default function TaskDetailPage() {
  return (
    <Suspense fallback={<p className="muted">加载中...</p>}>
      <TaskDetailContent />
    </Suspense>
  );
}
