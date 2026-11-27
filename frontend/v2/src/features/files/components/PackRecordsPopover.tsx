import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { EmptyState } from "@/shared/components/EmptyState";
import { Icon, type IconName } from "@/shared/components/Icon";
import { useMounted } from "@/shared/hooks/useMounted";
import { formatBytes } from "@/shared/lib/format";
import type { PackViewResponse } from "@/shared/api/types";
import { formatFileDate } from "../model";

export type PackOperation = number | "clear" | null;

/** 收起动画时长（与 06-components.css 的 pack-pop-out 一致），animationend 未触发时兜底。 */
const CLOSE_FALLBACK_MS = 200;
/** 浮层与入口按钮的间距与视口最小边距。 */
const ANCHOR_GAP_PX = 8;
const VIEWPORT_MARGIN_PX = 12;
const MAX_HEIGHT_PX = 660;

type PackRecordsPopoverProps = {
  tasks: PackViewResponse[];
  loading: boolean;
  error: string | null;
  nowMs: number;
  operation: PackOperation;
  trigger: HTMLElement | null;
  closing: boolean;
  onRequestClose: () => void;
  onClosed: () => void;
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

function getStatusIcon(task: PackViewResponse): IconName {
  if (task.status === "packing") return "refresh";
  if (task.status === "pending") return "clock";
  if (isOutputDeleted(task)) return "archive";
  if (task.status === "done") return "check";
  if (task.status === "failed") return "alert";
  return "x";
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
  if (task.status === "failed") return task.error_message || "打包失败";
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
  // 进行中行省略日期（正在发生，日期是噪音），已结束行保留完成时间。
  const metadata = [
    getStatusText(task),
    detail,
    active ? null : formatFileDate(task.finished_at_ms ?? task.created_at_ms),
  ].filter(Boolean).join(" · ");
  // 失败行只展示中文原因，原始代号只留在悬停提示供排障。
  const errorHint = task.status === "failed" ? task.error_code ?? undefined : undefined;
  const rowStatus = isOutputDeleted(task) ? "deleted" : task.status;

  return (
    <article className="pack-record" data-pack-id={task.id} data-status={rowStatus}>
      <span className="file-mark"><Icon name={getStatusIcon(task)} /></span>
      <div className="pack-record-copy">
        <strong title={task.output_name}>{task.output_name}</strong>
        <span
          className={task.status === "failed" && !isOutputDeleted(task) ? "pack-record-meta is-error" : "pack-record-meta"}
          title={errorHint}
        >
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

export function PackRecordsPopover(props: PackRecordsPopoverProps) {
  const mounted = useMounted();
  // 静态渲染（SSR）不挂 portal；内部组件只在客户端 mounted 后渲染，可安全使用 useLayoutEffect。
  if (!mounted) return null;
  return <PackRecordsPopoverPortal {...props} />;
}

function PackRecordsPopoverPortal({
  tasks,
  loading,
  error,
  nowMs,
  operation,
  trigger,
  closing,
  onRequestClose,
  onClosed,
  onRetry,
  onClear,
  onCancel,
  onDelete,
  onDownload,
}: PackRecordsPopoverProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const finishedRef = useRef(false);

  const finish = useCallback(() => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    // 焦点在浮层内被卸载时会丢到 body，还给入口按钮；浮层外（点击穿透）不抢夺焦点。
    const root = rootRef.current;
    if (root && document.activeElement instanceof HTMLElement && root.contains(document.activeElement)) {
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    }
    onClosed();
  }, [onClosed, trigger]);

  useEffect(() => {
    if (!closing) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      finish();
      return undefined;
    }
    const root = rootRef.current;
    const timer = window.setTimeout(finish, CLOSE_FALLBACK_MS);
    const handleAnimationEnd = (event: AnimationEvent) => {
      if (event.target === root) finish();
    };
    root?.addEventListener("animationend", handleAnimationEnd);
    return () => {
      window.clearTimeout(timer);
      root?.removeEventListener("animationend", handleAnimationEnd);
    };
  }, [closing, finish]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // 上层还有模态（如行内操作的确认框）时，Escape 交给模态处理。
      if (document.querySelector("dialog[open]")) return;
      onRequestClose();
    };
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (rootRef.current?.contains(target)) return;
      if (trigger?.contains(target)) return;
      // 模态（确认框、其他对话框）内部或背板上的点击不影响浮层。
      if (target.closest("dialog")) return;
      // 不拦截事件：点击穿透到页面元素正常生效，浮层只负责收起。
      onRequestClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown, true);
    };
  }, [onRequestClose, trigger]);

  useLayoutEffect(() => {
    const place = () => {
      const root = rootRef.current;
      if (!root || !trigger) return;
      const rect = trigger.getBoundingClientRect();
      const top = Math.round(rect.bottom + ANCHOR_GAP_PX);
      root.style.top = `${top}px`;
      root.style.maxHeight = `${Math.max(200, Math.min(MAX_HEIGHT_PX, window.innerHeight - top - VIEWPORT_MARGIN_PX))}px`;
      const width = root.offsetWidth;
      const left = Math.max(
        VIEWPORT_MARGIN_PX,
        Math.min(rect.right - width, window.innerWidth - width - VIEWPORT_MARGIN_PX),
      );
      root.style.left = `${left}px`;
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [trigger]);

  const active = tasks.filter((task) => task.status === "pending" || task.status === "packing");
  const finished = tasks.filter((task) => task.status === "done" || task.status === "failed" || task.status === "cancelled");
  const rowProps = { nowMs, operation, onCancel, onDelete, onDownload };

  return createPortal(
    <div
      ref={rootRef}
      className={closing ? "pack-pop is-closing" : "pack-pop is-open"}
      role="dialog"
      aria-label="打包记录"
    >
      <div className="pack-pop-body">
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
          tasks.length === 0 ? (
            <EmptyState
              icon={<Icon name="archive" className="icon-lg" />}
              title="还没有打包记录"
              description="在文件列表选择文件后点击“打包”，记录会出现在这里。"
            />
          ) : (
            <div className="pack-records">
              {active.map((task) => <PackRecordRow key={task.id} task={task} {...rowProps} />)}
              {finished.map((task) => <PackRecordRow key={task.id} task={task} {...rowProps} />)}
            </div>
          )
        )}
      </div>
      <div className="pack-pop-footer">
        <button
          type="button"
          className="button secondary btn-sm"
          onClick={onClear}
          disabled={finished.length === 0 || operation !== null}
          aria-busy={operation === "clear"}
        >
          {operation === "clear" ? "清空中..." : "清空已结束"}
        </button>
      </div>
    </div>,
    document.body,
  );
}
