import { createPortal } from "react-dom";

import { EmptyState } from "@/shared/components/EmptyState";
import { Icon } from "@/shared/components/Icon";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { useMounted } from "@/shared/hooks/useMounted";
import { formatBytes } from "@/shared/lib/format";
import { requestModalClose } from "@/shared/ui/modalClose";
import type { PackViewResponse } from "@/shared/api/types";
import { formatFileDate } from "../model";

export type PackOperation = number | "clear" | null;

type PackRecordsDrawerProps = {
  tasks: PackViewResponse[];
  loading: boolean;
  error: string | null;
  nowMs: number;
  operation: PackOperation;
  trigger: Element | null;
  onClose: () => void;
  onRetry: () => void;
  onClear: () => void;
  onCancel: (task: PackViewResponse) => void;
  onDelete: (task: PackViewResponse) => void;
  onDownload: (task: PackViewResponse, output: NonNullable<PackViewResponse["output"]>) => void;
};

function isOutputDeleted(task: PackViewResponse): boolean {
  return (task.status === "done" && task.output === null)
    || (task.status === "failed" && task.error_code === "pack_output_deleted");
}

function getStatusText(task: PackViewResponse): string {
  if (isOutputDeleted(task)) return "产物已删除";
  switch (task.status) {
    case "pending": return "排队中";
    case "packing": return "打包中";
    case "done": return "已完成";
    case "failed": return "失败";
    case "cancelled": return "已取消";
  }
}

function getStepText(step: PackViewResponse["step"]): string {
  switch (step) {
    case "validating": return "校验";
    case "compressing": return "压缩";
    case "verifying": return "验收";
    default: return "处理中";
  }
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}分${remainingSeconds.toString().padStart(2, "0")}秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours}小时${(minutes % 60).toString().padStart(2, "0")}分${remainingSeconds.toString().padStart(2, "0")}秒`;
}

function getPackingDetail(task: PackViewResponse, nowMs: number): string {
  const prefix = `${getStepText(task.step)} · ${task.step_progress}%`;
  const startedMs = task.step_started_at_ms;
  if (
    startedMs === null
    || !Number.isFinite(task.step_progress)
    || task.step_progress <= 0
    || task.step_progress > 100
  ) {
    return `${prefix} · 已用 -- / 预计剩余 --`;
  }
  const elapsed = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  if (elapsed <= 0) return `${prefix} · 已用 -- / 预计剩余 --`;
  const eta = Math.max(0, Math.round(elapsed * (100 - task.step_progress) / task.step_progress));
  return `${prefix} · 已用 ${formatDuration(elapsed)} / 预计剩余 ${formatDuration(eta)}`;
}

function getRecordDetail(task: PackViewResponse, nowMs: number): string | null {
  if (isOutputDeleted(task)) return null;
  if (task.status === "pending") return `${task.step_progress}%`;
  if (task.status === "packing") return getPackingDetail(task, nowMs);
  if (task.status === "done" && task.output) {
    return `${formatBytes(task.output.size)}${task.delete_source ? " · 已删除源文件" : ""}`;
  }
  if (task.status === "failed") return task.error_code;
  return null;
}

type PackRecordRowProps = {
  task: PackViewResponse;
  nowMs: number;
  operation: PackOperation;
  onCancel: (task: PackViewResponse) => void;
  onDelete: (task: PackViewResponse) => void;
  onDownload: (task: PackViewResponse, output: NonNullable<PackViewResponse["output"]>) => void;
};

function PackRecordRow({ task, nowMs, operation, onCancel, onDelete, onDownload }: PackRecordRowProps) {
  const active = task.status === "pending" || task.status === "packing";
  const busy = operation !== null;
  const detail = getRecordDetail(task, nowMs);
  const downloadableOutput = task.status === "done" ? task.output : null;
  const metadata = [
    getStatusText(task),
    detail,
    formatFileDate(task.finished_at_ms ?? task.created_at_ms),
  ].filter(Boolean).join(" · ");

  return (
    <article className="pack-record" data-pack-id={task.id}>
      <span className="file-mark"><Icon name="archive" /></span>
      <div className="pack-record-copy">
        <strong title={task.output_name}>{task.output_name}</strong>
        <span className={task.status === "failed" && !isOutputDeleted(task) ? "pack-record-meta is-error" : "pack-record-meta"}>
          {metadata}
        </span>
        {active && (
          <div
            className="pack-progress-bar"
            role="progressbar"
            aria-label={`${task.output_name} 打包进度`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={task.step_progress}
          >
            <div className="pack-progress-fill" style={{ width: `${task.step_progress}%` }} />
          </div>
        )}
      </div>
      <div className="pack-record-actions">
        {active ? (
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={() => onCancel(task)}
            disabled={busy}
            aria-busy={operation === task.id}
          >
            {operation === task.id ? "取消中..." : "取消"}
          </button>
        ) : (
          <>
            {downloadableOutput && (
              <button
                type="button"
                className="icon-btn"
                aria-label={`下载 ${task.output_name}`}
                title="下载"
                onClick={() => onDownload(task, downloadableOutput)}
                disabled={busy}
              >
                <Icon name="download" className="icon-sm" />
              </button>
            )}
            <button
              type="button"
              className="icon-btn danger"
              aria-label={`删除 ${task.output_name} 记录`}
              title={operation === task.id ? "删除中..." : "删除记录"}
              onClick={() => onDelete(task)}
              disabled={busy}
              aria-busy={operation === task.id}
            >
              <Icon name="trash" className="icon-sm" />
            </button>
          </>
        )}
      </div>
    </article>
  );
}

export function PackRecordsDrawer({
  tasks,
  loading,
  error,
  nowMs,
  operation,
  trigger,
  onClose,
  onRetry,
  onClear,
  onCancel,
  onDelete,
  onDownload,
}: PackRecordsDrawerProps) {
  const mounted = useMounted();
  if (!mounted) return null;

  const active = tasks.filter((task) => task.status === "pending" || task.status === "packing");
  const finished = tasks.filter((task) => task.status === "done" || task.status === "failed" || task.status === "cancelled");
  const rowProps = { nowMs, operation, onCancel, onDelete, onDownload };

  return createPortal(
    <ModalOverlay
      onClose={onClose}
      ariaLabel="打包记录"
      motionVariant="drawer"
      contentClassName="pack-records-drawer"
      trigger={trigger}
    >
      <div className="drawer-header">
        <h2>打包记录</h2>
        <button
          type="button"
          className="icon-btn"
          aria-label="关闭打包记录"
          onClick={(event) => requestModalClose(event.currentTarget)}
        >
          <span><Icon name="x" /></span>
        </button>
      </div>
      <div className="drawer-body pack-records-body">
        {error && (
          <div className="pack-records-error" role="alert">
            <span>加载失败：{error}</span>
            <button type="button" className="button secondary btn-sm" onClick={onRetry} disabled={loading}>
              {loading ? "重试中..." : "重试"}
            </button>
          </div>
        )}
        {loading && tasks.length === 0 ? (
          <p className="muted pack-records-loading" role="status">正在读取打包记录...</p>
        ) : (tasks.length > 0 || !error) && (
          <div className="pack-records">
            {tasks.length === 0 ? (
              <EmptyState
                icon={<Icon name="archive" className="icon-lg" />}
                title="还没有打包记录"
                description="在文件列表选择文件后点击“打包”，记录会出现在这里。"
              />
            ) : (
              <>
                {active.length > 0 && (
                  <>
                    <h3 className="pack-section-label">进行中 · {active.length}</h3>
                    {active.map((task) => <PackRecordRow key={task.id} task={task} {...rowProps} />)}
                  </>
                )}
                {finished.length > 0 && (
                  <>
                    <h3 className="pack-section-label">已结束 · {finished.length}</h3>
                    {finished.map((task) => <PackRecordRow key={task.id} task={task} {...rowProps} />)}
                  </>
                )}
              </>
            )}
          </div>
        )}
      </div>
      <div className="drawer-actions">
        <button
          type="button"
          className="button secondary"
          onClick={onClear}
          disabled={finished.length === 0 || operation !== null}
          aria-busy={operation === "clear"}
        >
          {operation === "clear" ? "清空中..." : "清空已结束"}
        </button>
        <button
          type="button"
          className="button primary"
          onClick={(event) => requestModalClose(event.currentTarget)}
        >
          关闭
        </button>
      </div>
    </ModalOverlay>,
    document.body,
  );
}
