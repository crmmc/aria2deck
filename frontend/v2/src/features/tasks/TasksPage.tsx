import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth, useToast } from "@/app/hooks";
import type { TaskResponse, TorrentPreviewResponse } from "@/shared/api/types";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Pagination,
} from "@/shared/components/ui";
import { useTaskWebSocket } from "@/shared/hooks/useTaskWebSocket";
import { ApiRequestError, errorMessage } from "@/shared/api/client";
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
import { FailureDialog, TorrentDialog, UriCreateDialog } from "./CreateDialogs";
import { TaskTable } from "./TaskTable";

const PAGE_SIZE = 20;
const FILTERS: { key: TaskFilter; label: string }[] = [
  { key: "current", label: "进行中" },
  { key: "active", label: "下载中" },
  { key: "complete", label: "已完成" },
  { key: "error", label: "异常" },
];

export function TasksPage() {
  const { user, logout } = useAuth();
  const { push } = useToast();
  const [tasks, setTasks] = useState<TaskResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<TaskFilter>("current");
  const [sort, setSort] = useState<TaskSort | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [dialog, setDialog] = useState<
    | { kind: "uri" }
    | { kind: "torrent"; preview: TorrentPreviewResponse }
    | { kind: "failures"; items: { uri: string; message: string }[] }
    | null
  >(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchTasks(filter, page, PAGE_SIZE, sort);
      if (seq !== requestSeq.current) return; // 丢弃过期响应。
      setTasks(response.items);
      setTotal(response.total);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(caught, "加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [filter, page, sort]);

  useEffect(() => {
    void load();
  }, [load]);

  // WS 实时更新：只写服务端副本（带过期丢弃），不改筛选/页码。
  const handleFrame = useCallback(
    (frame: { task: TaskResponse }) => {
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
    [load],
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

  const handleCancel = async (ids: number[]) => {
    try {
      const response = await cancelTasks(ids);
      const failed = response.items.filter((item) => !item.accepted);
      if (failed.length > 0) {
        push("error", `${failed.length} 个任务取消失败`);
      } else {
        push("success", "已取消");
      }
      setSelected(new Set());
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "取消失败"));
    }
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

  const liveIds = useMemo(
    () => tasks.filter((task) => task.status === "active").length,
    [tasks],
  );

  return (
    <section className="page" aria-label="任务列表">
      <header className="page-header">
        <h1>任务</h1>
        <span className="header-meta" aria-live="polite">
          {connected ? "实时连接中" : "离线轮询"} · 下载中 {liveIds}
        </span>
        <div className="header-actions">
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
          <button
            type="button"
            className="secondary-button"
            onClick={() => void handleCancel([...selected])}
          >
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
            sort={sort}
            onToggle={toggle}
            onToggleAll={toggleAll}
            onCancel={(id) => void handleCancel([id])}
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
        <TorrentDialog
          preview={dialog.preview}
          onClose={() => setDialog(null)}
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
