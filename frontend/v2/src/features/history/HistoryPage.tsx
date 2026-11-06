import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import type { HistoryStatus, TaskResponse } from "@/shared/api/types";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Pagination,
  StatusBadge,
} from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { formatBytes, formatDateTime } from "@/shared/lib/format";
import { deleteHistory, fetchHistory, retryFromHistory } from "./api";

function CopyUriButton({ uri, onCopy }: { uri: string; onCopy: (uri: string) => Promise<void> }) {
  return (
    <button type="button" className="link-button" onClick={() => void onCopy(uri)}>
      复制链接
    </button>
  );
}

/** H4 失败原因：下载侧已知 error_code 的中文口径，未知码原样展示。 */
const ERROR_CODE_LABEL: Record<string, string> = {
  upstream_failure: "上游下载失败",
  download_unavailable: "下载不可用",
  rpc_unavailable: "aria2 连接不可用",
  aria2_rejected: "aria2 拒绝了操作",
  task_too_large: "任务超出大小限制",
  quota_exceeded: "存储配额不足",
  physical_space_exceeded: "机器空间不足",
  submission_uncertain: "提交结果不可确认",
  recovery_unsafe: "恢复不安全",
  invalid_input: "输入无效",
  invalid_state: "状态无效",
  content_busy: "内容被占用",
};

function failureReason(item: TaskResponse): string | null {
  if (item.status !== "failed" && item.status !== "cancelled" && item.status !== "expired") {
    return null;
  }
  if (item.error_code === null) return null;
  return ERROR_CODE_LABEL[item.error_code] ?? item.error_code;
}

const PAGE_SIZE = 20;
const STATUS_OPTIONS: { value: HistoryStatus | ""; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
  { value: "expired", label: "已过期" },
];

export function HistoryPage() {
  const { push } = useToast();
  const [items, setItems] = useState<TaskResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<HistoryStatus | "">("");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search, 300);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirm, setConfirm] = useState<number[] | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchHistory(
        status === "" ? null : status,
        debouncedSearch,
        page,
        PAGE_SIZE,
      );
      if (seq !== requestSeq.current) return; // 丢弃过期响应。
      setItems(response.items);
      setTotal(response.total);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(caught, "加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [status, debouncedSearch, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const copyUri = async (uri: string) => {
    try {
      await navigator.clipboard.writeText(uri);
      push("success", "已复制链接");
    } catch {
      push("error", "复制失败，请手动复制");
    }
  };

  const handleDelete = async (ids: number[]) => {
    setConfirm(null);
    try {
      const response = await deleteHistory(ids);
      if (response.rejected_count > 0) {
        push("error", `${response.rejected_count} 条删除失败`);
      } else {
        push("success", "已删除");
      }
      // 删空末页回退：当前页只剩被删项时回上一页。
      const remaining = total - response.accepted_count;
      const maxPage = Math.max(1, Math.ceil(remaining / PAGE_SIZE));
      if (page > maxPage) setPage(maxPage);
      else await load();
      setSelected(new Set());
    } catch (caught) {
      push("error", errorMessage(caught, "删除失败"));
    }
  };

  const handleRetry = async (id: number) => {
    try {
      await retryFromHistory(id);
      push("success", "重试任务已创建");
    } catch (caught) {
      push("error", errorMessage(caught, "重试失败"));
    }
  };

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <section className="page" aria-label="历史记录">
      <header className="page-header">
        <h1>历史</h1>
        <div className="header-actions">
          <label className="search-field">
            <span className="visually-hidden">搜索历史</span>
            <input
              type="search"
              value={search}
              onChange={(event) => {
                // 条件变化与页码重置在同一事件内，避免旧页码双请求。
                setSearch(event.target.value);
                setPage(1);
              }}
              placeholder="搜索名称或链接…"
              aria-label="搜索历史"
            />
          </label>
          <label className="status-select">
            <span className="visually-hidden">状态筛选</span>
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as HistoryStatus | "");
                setPage(1);
              }}
              aria-label="状态筛选"
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          {selected.size > 0 && (
            <button
              type="button"
              className="danger-button"
              onClick={() => setConfirm([...selected])}
            >
              删除选中（{selected.size}）
            </button>
          )}
        </div>
      </header>

      {loading ? (
        <LoadingState />
      ) : error !== null ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="暂无历史记录"
          hint={debouncedSearch !== "" ? "换个关键词试试" : "完成的任务会出现在这里"}
        />
      ) : (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col" className="select-col">
                  <input
                    type="checkbox"
                    aria-label="全选历史"
                    checked={items.length > 0 && items.every((item) => selected.has(item.id))}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? new Set(items.map((item) => item.id))
                          : new Set(),
                      )
                    }
                  />
                </th>
                <th scope="col">名称</th>
                <th scope="col">状态</th>
                <th scope="col">大小</th>
                <th scope="col">失败原因</th>
                <th scope="col">完成时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} data-testid={`history-row-${item.id}`}>
                  <td className="select-col">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${item.name}`}
                      checked={selected.has(item.id)}
                      onChange={() => toggle(item.id)}
                    />
                  </td>
                  <td className="name-cell" title={item.name}>
                    {item.name}
                  </td>
                  <td>
                    <StatusBadge status={item.status} label={item.status_label} />
                  </td>
                  <td>{item.total_bytes === null ? "—" : formatBytes(item.total_bytes)}</td>
                  <td className="name-cell">
                    {(() => {
                      const reason = failureReason(item);
                      return reason === null ? "—" : <span title={reason}>{reason}</span>;
                    })()}
                  </td>
                  <td>{formatDateTime(item.finished_at_ms ?? item.updated_at_ms)}</td>
                  <td className="actions-cell">
                    {item.uri !== null && (
                      <CopyUriButton uri={item.uri} onCopy={copyUri} />
                    )}
                    {item.retryable && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => void handleRetry(item.id)}
                      >
                        重试
                      </button>
                    )}
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setConfirm([item.id])}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
        </>
      )}

      {confirm !== null && (
        <ConfirmDialog
          title="删除历史记录"
          message={`确定删除选中的 ${confirm.length} 条历史记录？删除后不可恢复。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleDelete(confirm)}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  );
}
