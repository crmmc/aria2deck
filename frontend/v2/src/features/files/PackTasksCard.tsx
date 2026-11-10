import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import { ConfirmDialog } from "@/shared/components/ui";
import { formatBytes, formatDuration } from "@/shared/lib/format";
import type { PackViewResponse } from "@/shared/api/types";
import { cancelPack, deletePackRecord, fetchPacks } from "./api";

/** F1 常驻打包任务卡：进行中 2s 轮询、步骤进度/ETA、取消与记录管理、完成自动刷新列表。 */

const PACK_POLL_MS = 2000;
const TICK_MS = 1000;
const PACK_LIMIT = 10;
const ACTIVE_STATUSES = ["pending", "packing"];
const TERMINAL_STATUSES = ["done", "failed", "cancelled"];
const PACK_OUTPUT_DELETED = "pack_output_deleted";

const STATUS_LABEL: Record<string, string> = {
  pending: "排队中",
  packing: "打包中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_LABEL: Record<string, string> = {
  validating: "校验",
  compressing: "压缩",
  verifying: "验收",
};

function packTitle(pack: PackViewResponse): string {
  if (pack.output !== null) return pack.output.display_name;
  return pack.output_name || `打包任务 #${pack.id}`;
}

/** 产物已不可用：物理回收后的专用失败历史，或共享引用下当前用户已删除的完成记录。 */
function isOutputDeleted(pack: PackViewResponse): boolean {
  return (
    (pack.status === "failed" && pack.error_code === PACK_OUTPUT_DELETED) ||
    (pack.status === "done" && pack.output === null)
  );
}

function statusTone(pack: PackViewResponse): string {
  if (isOutputDeleted(pack)) return "muted";
  if (pack.status === "done") return "ok";
  if (pack.status === "failed") return "error";
  if (pack.status === "packing" || pack.status === "pending") return "info";
  return "muted";
}

function stepTiming(pack: PackViewResponse, nowMs: number): string {
  const step = pack.step === null ? "处理中" : STEP_LABEL[pack.step] ?? "处理中";
  const started = pack.step_started_at_ms;
  if (
    started === null ||
    !Number.isFinite(started) ||
    pack.step_progress <= 0 ||
    pack.step_progress > 100
  ) {
    return `${step} · 已用 -- / 预计剩余 --`;
  }
  const elapsedSeconds = Math.max(0, Math.floor((nowMs - started) / 1000));
  if (elapsedSeconds <= 0) return `${step} · 已用 -- / 预计剩余 --`;
  const etaSeconds = Math.round((elapsedSeconds * (100 - pack.step_progress)) / pack.step_progress);
  return `${step} · 已用 ${formatDuration(elapsedSeconds * 1000)} / 预计剩余 ${formatDuration(etaSeconds * 1000)}`;
}

export function PackTasksCard({
  refreshKey,
  onTaskComplete,
}: {
  /** 弹窗关闭等外部事件通过递增该键触发卡片重载。 */
  refreshKey: number;
  onTaskComplete: () => void;
}) {
  const { push } = useToast();
  const [packs, setPacks] = useState<PackViewResponse[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [confirm, setConfirm] = useState<
    { kind: "cancel"; pack: PackViewResponse } | { kind: "clear" } | null
  >(null);
  const seq = useRef(0);
  const statusRef = useRef<Map<number, string>>(new Map());
  const onCompleteRef = useRef(onTaskComplete);
  onCompleteRef.current = onTaskComplete;

  const load = useCallback(async () => {
    const current = ++seq.current;
    try {
      const response = await fetchPacks(PACK_LIMIT);
      if (current !== seq.current) return; // 丢弃过期响应。
      // 检测进行中 → 完成的迁移，触发文件列表刷新。
      let completed = false;
      for (const pack of response.items) {
        const previous = statusRef.current.get(pack.id);
        if (pack.status === "done" && previous !== undefined && ACTIVE_STATUSES.includes(previous)) {
          completed = true;
        }
      }
      statusRef.current = new Map(response.items.map((pack) => [pack.id, pack.status]));
      setPacks(response.items);
      if (completed) onCompleteRef.current();
    } catch {
      /* 轮询失败保留下次重试 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const hasActive = useMemo(
    () => packs.some((pack) => ACTIVE_STATUSES.includes(pack.status)),
    [packs],
  );

  // 进行中任务：2s 轮询（页面隐藏跳过）+ 1s 计时刷新 ETA。
  useEffect(() => {
    if (!hasActive) return;
    const poll = window.setInterval(() => {
      if (!document.hidden) void load();
    }, PACK_POLL_MS);
    const tick = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, [hasActive, load]);

  const handleCancel = async () => {
    const target = confirm;
    setConfirm(null);
    if (target === null || target.kind !== "cancel") return;
    try {
      await cancelPack(target.pack.id);
      push("success", "已请求取消打包任务");
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "取消失败"));
    }
  };

  const handleClear = async () => {
    setConfirm(null);
    const records = packs.filter((pack) => TERMINAL_STATUSES.includes(pack.status));
    let cleared = 0;
    let failed = 0;
    for (const pack of records) {
      try {
        await deletePackRecord(pack.id);
        cleared += 1;
      } catch {
        failed += 1;
      }
    }
    if (failed > 0) push("error", `已清空 ${cleared} 条记录，${failed} 条清空失败`);
    else push("success", `已清空 ${cleared} 条记录`);
    await load();
  };

  const handleDeleteRecord = async (pack: PackViewResponse) => {
    try {
      await deletePackRecord(pack.id);
      push("success", "已删除任务记录");
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "删除记录失败"));
    }
  };

  if (packs.length === 0) return null;
  const activeCount = packs.filter((pack) => ACTIVE_STATUSES.includes(pack.status)).length;
  const terminalCount = packs.length - activeCount;

  return (
    <section className="section-card pack-card" aria-label="打包任务">
      <div className="pack-card-header">
        <h2 className="section-title">
          打包任务
          {activeCount > 0 && (
            <span className="badge tone-info password-badge">进行中 {activeCount}</span>
          )}
        </h2>
        {terminalCount > 0 && (
          <button
            type="button"
            className="secondary-button"
            onClick={() => setConfirm({ kind: "clear" })}
          >
            清空已完成
          </button>
        )}
      </div>
      <ul className="pack-card-list">
        {packs.map((pack) => (
          <li key={pack.id} className="pack-card-item" data-testid={`pack-item-${pack.id}`}>
            <div className="pack-card-row">
              <span className="pack-card-name" title={packTitle(pack)}>
                {packTitle(pack)}
              </span>
              <span className={`badge tone-${statusTone(pack)}`}>
                {isOutputDeleted(pack) ? "产物已删除" : STATUS_LABEL[pack.status] ?? pack.status}
              </span>
            </div>
            {ACTIVE_STATUSES.includes(pack.status) && (
              <>
                <div
                  className="progress-track"
                  role="progressbar"
                  aria-label={`${packTitle(pack)} 打包进度`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={pack.status === "pending" ? 0 : pack.step_progress}
                >
                  <div
                    className={`progress-fill${pack.status === "packing" ? " progress-active" : ""}`}
                    style={{ width: `${pack.status === "pending" ? 0 : pack.step_progress}%` }}
                  />
                </div>
                <div className="pack-card-row">
                  <span className="hint-text">
                    {pack.status === "pending" ? "排队中" : stepTiming(pack, nowMs)}
                  </span>
                  <button
                    type="button"
                    className="link-button danger-link"
                    onClick={() => setConfirm({ kind: "cancel", pack })}
                  >
                    取消
                  </button>
                </div>
              </>
            )}
            {pack.status === "done" && !isOutputDeleted(pack) && (
              <div className="pack-card-row">
                <span className="hint-text">
                  输出：{formatBytes(pack.output?.size ?? 0)}
                  {pack.delete_source && " · 已删除源文件"}
                </span>
                <button
                  type="button"
                  className="link-button danger-link"
                  onClick={() => void handleDeleteRecord(pack)}
                >
                  删除记录
                </button>
              </div>
            )}
            {isOutputDeleted(pack) && (
              <div className="pack-card-row">
                <button
                  type="button"
                  className="link-button danger-link"
                  onClick={() => void handleDeleteRecord(pack)}
                >
                  删除记录
                </button>
              </div>
            )}
            {(pack.status === "failed" || pack.status === "cancelled") && !isOutputDeleted(pack) && (
              <div className="pack-card-row">
                {pack.status === "failed" && pack.error_code !== null && (
                  <span className="hint-text">错误码：{pack.error_code}</span>
                )}
                <button
                  type="button"
                  className="link-button danger-link"
                  onClick={() => void handleDeleteRecord(pack)}
                >
                  删除记录
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {confirm?.kind === "cancel" && (
        <ConfirmDialog
          title="取消打包任务"
          message={`确定要取消「${packTitle(confirm.pack)}」吗？已产生的临时文件会被清理。`}
          confirmLabel="取消任务"
          danger
          onConfirm={() => void handleCancel()}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm?.kind === "clear" && (
        <ConfirmDialog
          title="清空已完成"
          message={`确定要清空 ${terminalCount} 条已完成的打包记录吗？`}
          confirmLabel="清空"
          danger
          onConfirm={() => void handleClear()}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  );
}
