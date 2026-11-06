import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth, useToast } from "@/app/hooks";
import type { TaskResponse, TorrentPreviewResponse } from "@/shared/api/types";
import { ConfirmDialog, EmptyState, ErrorState, LoadingState, Pagination } from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { useTaskWebSocket } from "@/shared/hooks/useTaskWebSocket";
import { ApiRequestError, errorMessage } from "@/shared/api/client";
import { copyText } from "@/shared/lib/clipboard";
import { emitTaskActivity } from "@/shared/lib/taskEvents";
import { sendTaskNotification } from "@/shared/notifications";
import {
  cancelTasks,
  createTasks,
  createTorrent,
  fetchTasks,
  previewTorrent,
  retryTask,
  type CreateOutcome,
  type TaskFilter,
  type TaskSortKey,
  type TaskSort,
} from "./api";
import { FailureDialog, TorrentCreateWizard, UriCreateDialog } from "./CreateDialogs";
import { TaskTable } from "./TaskTable";

const PAGE_SIZE = 20;
const MAX_TORRENT_FILE_SIZE = 10 * 1024 * 1024;
const FILTERS: { key: TaskFilter; label: string }[] = [
  { key: "current", label: "进行中" },
  { key: "active", label: "下载中" },
  { key: "complete", label: "已完成" },
  { key: "error", label: "异常" },
];

const IN_FLIGHT_STATUSES = ["queued", "active", "waiting", "paused"];
const REMOVABLE_STATUSES = ["failed", "cancelled", "expired"];

type ConfirmState =
  | { kind: "cancel"; task: TaskResponse }
  | { kind: "delete"; task: TaskResponse }
  | { kind: "batch-cancel"; ids: number[] }
  | null;

export function TasksPage() {
  const { user, logout } = useAuth();
  const { push } = useToast();
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<TaskFilter>("current");
  const [sort, setSort] = useState<TaskSort | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const search = useDebouncedValue(searchInput, 300);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [dialog, setDialog] = useState<
    | { kind: "uri" }
    | { kind: "torrent"; preview: TorrentPreviewResponse }
    | { kind: "failures"; items: { uri: string; message: string }[] }
    | null
  >(null);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const requestSeq = useRef(0);
  const tasksRef = useRef<TaskResponse[]>([]);
  tasksRef.current = tasks;

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchTasks(filter, page, PAGE_SIZE, sort, search);
      if (seq !== requestSeq.current) return; // 丢弃过期响应。
      setTasks(response.items);
      setTotal(response.total);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(caught, "加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [filter, page, sort, search]);

  useEffect(() => {
    void load();
  }, [load]);

  // WS 实时更新：只写服务端副本（带过期丢弃），不改筛选/页码；完成/失败迁移触发 Toast+桌面通知（T15）。
  const handleFrame = useCallback(
    (frame: { task: TaskResponse }) => {
      const previous = tasksRef.current.find((item) => item.id === frame.task.id);
      if (previous !== undefined && previous.status !== frame.task.status) {
        emitTaskActivity(); // 布局统计立即刷新（G11）。
        const name = frame.task.name || "下载任务";
        if (frame.task.status === "completed") {
          push("success", `${name} 下载完成`);
          sendTaskNotification("completed", name, frame.task.id, push);
        } else if (frame.task.status === "failed") {
          push("error", `${name} 下载失败`);
          sendTaskNotification("failed", name, frame.task.id, push);
        }
      }
      setTasks((current) => {
        const next = [...current];
        const index = next.findIndex((item) => item.id === frame.task.id);
        if (index >= 0 && frame.task.status !== next[index].status) {
          // 当前页内的状态变化：直接刷新整页以保持筛选与分页一致。
          void load();
          return current;
        }
        if (index >= 0) next[index] = frame.task;
        return next;
      });
    },
    [load, push],
  );

  const { connected } = useTaskWebSocket(handleFrame, {
    enabled: user !== null,
    pollFallback: () => void load(),
    onUnauthorized: () => {
      void logout();
    },
  });

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // 表头排序循环：默认 → 升序 → 降序 → 还原默认；条件变化同步回第 1 页。
  const handleSort = (key: TaskSortKey) => {
    setPage(1);
    setSort((current) => {
      if (current?.key !== key) return { key, order: "asc" };
      if (current.order === "asc") return { key, order: "desc" };
      return null;
    });
  };

  const toggleAll = (checked: boolean) => {
    setSelected(checked ? new Set(tasks.map((task) => task.id)) : new Set());
  };

  const handleCreate = async (text: string): Promise<CreateOutcome | null> => {
    try {
      const outcome = await createTasks(text);
      if (outcome.rejectedCount > 0) {
        setDialog({ kind: "failures", items: outcome.failedItems });
      } else {
        push("success", `已添加 ${outcome.acceptedCount} 个任务`);
        setDialog(null);
      }
      setPage(1);
      await load();
      return outcome;
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.status === 502) {
        push("error", "下载结果暂无法确认，请稍后在任务列表核对");
        return null;
      }
      throw caught;
    }
  };

  // 仅重试失败项：载荷来自弹窗持有的本轮失败 URI，不重放原始全文。
  const handleRetryFailed = async (items: { uri: string; message: string }[]) => {
    setDialog(null);
    if (items.length === 0) return;
    try {
      const outcome = await createTasks(items.map((item) => item.uri).join("\n"));
      if (outcome.rejectedCount > 0) {
        // 新弹窗只包含本轮失败项，避免失败集合跨轮累积。
        setDialog({ kind: "failures", items: outcome.failedItems });
      } else {
        push("success", `已添加 ${outcome.acceptedCount} 个任务`);
      }
      setPage(1);
      await load();
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.status === 502) {
        push("error", "下载结果暂无法确认，请稍后在任务列表核对");
      } else {
        push("error", errorMessage(caught, "重试失败"));
      }
    }
  };

  const handleTorrentSelect = async () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".torrent";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (file === undefined) return;
      // 种子上传前端校验（T7）：后缀 + 大小，错误信息与旧版一致。
      if (!file.name.endsWith(".torrent")) {
        push("error", "请选择 .torrent 文件");
        return;
      }
      if (file.size > MAX_TORRENT_FILE_SIZE) {
        push("error", "种子文件过大，最大支持 10 MB");
        return;
      }
      const base64 = await readFileBase64(file);
      try {
        const preview = await previewTorrent(base64);
        (window as unknown as { __torrentBase64?: string }).__torrentBase64 = base64;
        setDialog({ kind: "torrent", preview });
      } catch (caught) {
        push("error", errorMessage(caught, "种子解析失败"));
      }
    };
    input.click();
  };

  const handleTorrentCreate = async (selectedIndexes: number[]) => {
    const base64 = (window as unknown as { __torrentBase64?: string }).__torrentBase64;
    if (base64 === undefined) throw new Error("种子数据丢失，请重新选择");
    const outcome = await createTorrent(base64, selectedIndexes);
    if (outcome.rejectedCount > 0) {
      setDialog({ kind: "failures", items: outcome.failedItems });
    } else {
      push("success", "种子任务已创建");
      setDialog(null);
    }
    setPage(1);
    await load();
  };

  const handleTorrentClose = () => {
    delete (window as unknown as { __torrentBase64?: string }).__torrentBase64;
    setDialog(null);
  };

  const reportCancelOutcome = useCallback(
    (accepted: number, rejected: number, action: string) => {
      if (rejected > 0) {
        push("error", `已${action} ${accepted} 个任务，${rejected} 个${action}失败`);
      } else {
        push("success", `已${action} ${accepted} 个任务`);
      }
    },
    [push],
  );

  // 取消进行中任务（T10：danger 确认）。
  const performCancel = useCallback(
    async (ids: number[], action: string) => {
      try {
        const response = await cancelTasks(ids);
        reportCancelOutcome(
          response.accepted_count,
          response.rejected_count,
          action,
        );
        setSelected(new Set());
        await load();
      } catch (caught) {
        push("error", errorMessage(caught, `${action}失败`));
      }
    },
    [load, push, reportCancelOutcome],
  );

  const handleConfirm = () => {
    if (confirm === null) return;
    if (confirm.kind === "cancel") {
      void performCancel([confirm.task.id], "取消");
    } else if (confirm.kind === "delete") {
      void performCancel([confirm.task.id], "删除");
    } else {
      void performCancel(confirm.ids, "取消");
    }
    setConfirm(null);
  };

  // 批量取消（T11）：仅取消选中的进行中任务，先经确认弹窗。
  const requestBatchCancel = () => {
    const ids = tasks
      .filter((task) => selected.has(task.id) && IN_FLIGHT_STATUSES.includes(task.status))
      .map((task) => task.id);
    if (ids.length === 0) {
      push("info", "没有可取消的任务");
      return;
    }
    setConfirm({ kind: "batch-cancel", ids });
  };

  const handleRetry = async (id: number) => {
    try {
      await retryTask(id);
      push("success", "重试已创建");
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "重试失败"));
    }
  };

  // 复制链接（T12）：clipboard API + 旧环境降级，结果以 Toast 反馈。
  const handleCopyUri = useCallback(
    async (uri: string) => {
      const copied = await copyText(uri);
      if (copied) push("success", "链接已复制");
      else push("error", "复制失败，请手动复制");
    },
    [push],
  );

  const liveIds = useMemo(
    () => tasks.filter((task) => task.status === "active").length,
    [tasks],
  );

  const allSelected = tasks.length > 0 && tasks.every((task) => selected.has(task.id));
  const removableIds = useMemo(
    () => new Set(tasks.filter((task) => REMOVABLE_STATUSES.includes(task.status)).map((t) => t.id)),
    [tasks],
  );

  const confirmDialog =
    confirm === null ? null : confirm.kind === "cancel" ? (
      <ConfirmDialog
        title="取消下载"
        message={`确定要取消下载「${confirm.task.name}」吗？`}
        confirmLabel="取消下载"
        danger
        onConfirm={handleConfirm}
        onCancel={() => setConfirm(null)}
      />
    ) : confirm.kind === "delete" ? (
      <ConfirmDialog
        title="删除任务"
        message={`确定要删除任务「${confirm.task.name}」吗？`}
        confirmLabel="删除"
        danger
        onConfirm={handleConfirm}
        onCancel={() => setConfirm(null)}
      />
    ) : (
      <ConfirmDialog
        title="批量取消"
        message={`确定要取消选中的 ${confirm.ids.length} 个任务吗？`}
        confirmLabel="确认取消"
        danger
        onConfirm={handleConfirm}
        onCancel={() => setConfirm(null)}
      />
    );

  return (
    <section className="page" aria-label="任务列表">
      <header className="page-header">
        <h1>任务</h1>
        <span className="header-meta" aria-live="polite">
          {connected ? "实时连接中" : "离线轮询"} · 下载中 {liveIds}
        </span>
        <div className="header-actions">
          <input
            type="search"
            className="task-search"
            placeholder="搜索任务名称"
            aria-label="搜索任务"
            value={searchInput}
            onChange={(event) => {
              setSearchInput(event.target.value);
              setPage(1); // 搜索条件变化同步回第 1 页。
            }}
          />
          <button
            type="button"
            className="secondary-button"
            onClick={() => void handleTorrentSelect()}
          >
            种子下载
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => setDialog({ kind: "uri" })}
          >
            新建任务
          </button>
        </div>
      </header>

      <div className="filter-bar" role="tablist" aria-label="状态筛选">
        {FILTERS.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={filter === item.key}
            className={`filter-chip${filter === item.key ? " active" : ""}`}
            onClick={() => {
              setFilter(item.key);
              setPage(1);
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span>已选 {selected.size} 项</span>
          <button type="button" className="secondary-button" onClick={requestBatchCancel}>
            批量取消
          </button>
          <button
            type="button"
            className="link-button"
            onClick={() => setSelected(new Set())}
          >
            清除选择
          </button>
        </div>
      )}

      {loading ? (
        <LoadingState />
      ) : error !== null ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : tasks.length === 0 ? (
        <EmptyState
          title="暂无任务"
          hint="点击右上角「新建任务」添加 HTTP(S) 或 Magnet 链接，或选择种子文件。"
        />
      ) : (
        <>
          <TaskTable
            tasks={tasks}
            selected={selected}
            allSelected={allSelected}
            removableIds={removableIds}
            sort={sort}
            onToggle={toggle}
            onToggleAll={toggleAll}
            onRequestCancel={(task) => setConfirm({ kind: "cancel", task })}
            onRequestDelete={(task) => setConfirm({ kind: "delete", task })}
            onCopy={(uri) => void handleCopyUri(uri)}
            onRetry={(id) => void handleRetry(id)}
            onSort={handleSort}
          />
          <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
        </>
      )}

      {dialog?.kind === "uri" && (
        <UriCreateDialog onClose={() => setDialog(null)} onCreate={handleCreate} />
      )}
      {dialog?.kind === "torrent" && (
        <TorrentCreateWizard
          preview={dialog.preview}
          onClose={handleTorrentClose}
          onCreate={handleTorrentCreate}
        />
      )}
      {dialog?.kind === "failures" && (
        <FailureDialog
          failedItems={dialog.items}
          onRetryFailed={() => void handleRetryFailed(dialog.items)}
          onClose={() => setDialog(null)}
        />
      )}
      {confirmDialog}
    </section>
  );
}

async function readFileBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}
