import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Pagination,
} from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import { formatBytes, formatDateTime } from "@/shared/lib/format";
import type { ShareItemResponse, ShareItemStatus } from "@/shared/api/types";
import {
  deleteShares,
  fetchShares,
  revokeAllShares,
  revokeShares,
  shareCopyText,
} from "./api";

const PAGE_SIZE = 20;

const STATUS_OPTIONS: { value: ShareItemStatus | ""; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "active", label: "有效" },
  { value: "expired", label: "已过期" },
  { value: "revoked", label: "已撤销" },
  { value: "exhausted", label: "已耗尽" },
  { value: "unavailable", label: "文件已删除" },
];

const STATUS_LABEL: Record<ShareItemStatus, string> = {
  active: "有效",
  expired: "已过期",
  revoked: "已撤销",
  exhausted: "已耗尽",
  unavailable: "文件已删除",
};

const STATUS_TONE: Record<ShareItemStatus, string> = {
  active: "ok",
  expired: "muted",
  revoked: "muted",
  exhausted: "warn",
  unavailable: "error",
};

export function SharesPage() {
  const { push } = useToast();
  const [items, setItems] = useState<ShareItemResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<ShareItemStatus | "">("");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search, 300);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [confirm, setConfirm] = useState<
    { kind: "revoke" | "delete"; ids: number[] } | { kind: "revoke-all" } | null
  >(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      // 服务端搜索：q 由后端过滤，total 即筛选后的真实总数。
      const response = await fetchShares(
        status === "" ? null : status,
        page,
        PAGE_SIZE,
        debouncedSearch,
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
  }, [status, page, debouncedSearch]);

  useEffect(() => {
    void load();
  }, [load]);

  const copyLink = async (share: ShareItemResponse) => {
    try {
      await navigator.clipboard.writeText(shareCopyText(share));
      push("success", "已复制分享内容");
    } catch {
      push("error", "复制失败，请手动复制");
    }
  };

  const reportBatch = (label: string, response: { accepted_count: number; rejected_count: number }) => {
    if (response.rejected_count > 0) {
      push("error", `${label}完成：成功 ${response.accepted_count} 个，失败 ${response.rejected_count} 个`);
    } else {
      push("success", `${label}完成：${response.accepted_count} 个`);
    }
  };

  const handleRevoke = async (ids: number[]) => {
    setConfirm(null);
    try {
      reportBatch("撤销", await revokeShares(ids));
      setSelected(new Set());
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "撤销失败"));
    }
  };

  const handleDelete = async (ids: number[]) => {
    setConfirm(null);
    try {
      reportBatch("删除", await deleteShares(ids));
      const remaining = total - ids.length;
      const maxPage = Math.max(1, Math.ceil(remaining / PAGE_SIZE));
      if (page > maxPage) setPage(maxPage);
      else await load();
      setSelected(new Set());
    } catch (caught) {
      push("error", errorMessage(caught, "删除失败"));
    }
  };

  const handleRevokeAll = async () => {
    setConfirm(null);
    try {
      const response = await revokeAllShares();
      push("success", `已撤销 ${response.revoked_count} 个分享`);
      setPage(1);
      await load();
    } catch (caught) {
      push("error", errorMessage(caught, "撤销失败"));
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
    <section className="page" aria-label="分享管理">
      <header className="page-header">
        <h1>分享</h1>
        <div className="header-actions">
          <label className="search-field">
            <span className="visually-hidden">搜索分享</span>
            <input
              type="search"
              value={search}
              onChange={(event) => {
                // 条件变化与页码重置在同一事件内，避免旧页码双请求。
                setSearch(event.target.value);
                setPage(1);
              }}
              placeholder="搜索文件名或分享码…"
              aria-label="搜索分享"
            />
          </label>
          <label className="status-select">
            <span className="visually-hidden">状态筛选</span>
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as ShareItemStatus | "");
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
          <button
            type="button"
            className="danger-button"
            onClick={() => setConfirm({ kind: "revoke-all" })}
          >
            全部撤销
          </button>
        </div>
      </header>

      {selected.size > 0 && (
        <div className="bulk-bar">
          <span>已选 {selected.size} 项</span>
          <button
            type="button"
            className="secondary-button"
            onClick={() => setConfirm({ kind: "revoke", ids: [...selected] })}
          >
            批量撤销
          </button>
          <button
            type="button"
            className="danger-button"
            onClick={() => setConfirm({ kind: "delete", ids: [...selected] })}
          >
            批量删除
          </button>
          <button type="button" className="link-button" onClick={() => setSelected(new Set())}>
            清除选择
          </button>
        </div>
      )}

      {loading ? (
        <LoadingState />
      ) : error !== null ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="暂无分享"
          hint={debouncedSearch.trim() !== "" ? "换个关键词试试" : "在文件页选择文件创建分享链接。"}
        />
      ) : (
        <>
          <table className="data-table" aria-label="分享列表">
            <thead>
              <tr>
                <th scope="col" className="select-col">
                  <input
                    type="checkbox"
                    aria-label="全选分享"
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
                <th scope="col">文件名</th>
                <th scope="col">大小</th>
                <th scope="col">状态</th>
                <th scope="col">下载</th>
                <th scope="col">过期时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} data-testid={`share-row-${item.id}`}>
                  <td className="select-col">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${item.file_name}`}
                      checked={selected.has(item.id)}
                      onChange={() => toggle(item.id)}
                    />
                  </td>
                  <td className="name-cell" title={item.file_name}>
                    {item.file_name}
                    {item.has_password && (
                      <span className="badge tone-warn password-badge" title="需要提取密码">
                        密码
                      </span>
                    )}
                  </td>
                  <td>{formatBytes(item.file_size)}</td>
                  <td>
                    <span className={`badge tone-${STATUS_TONE[item.status]}`}>
                      {STATUS_LABEL[item.status]}
                    </span>
                  </td>
                  <td>
                    {item.max_downloads === null
                      ? `${item.download_count} / 不限`
                      : `${item.download_count} / ${item.max_downloads}`}
                  </td>
                  <td>{item.expires_at_ms === null ? "永久" : formatDateTime(item.expires_at_ms)}</td>
                  <td className="actions-cell">
                    <button type="button" className="link-button" onClick={() => void copyLink(item)}>
                      复制
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setConfirm({ kind: "revoke", ids: [item.id] })}
                    >
                      撤销
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setConfirm({ kind: "delete", ids: [item.id] })}
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

      {confirm?.kind === "revoke" && (
        <ConfirmDialog
          title="撤销分享"
          message={`确定撤销选中的 ${confirm.ids.length} 个分享？撤销后访问者将无法继续下载。`}
          confirmLabel="撤销"
          onConfirm={() => void handleRevoke(confirm.ids)}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm?.kind === "delete" && (
        <ConfirmDialog
          title="删除分享记录"
          message={`确定删除选中的 ${confirm.ids.length} 条分享记录？删除后不可恢复。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleDelete(confirm.ids)}
          onCancel={() => setConfirm(null)}
        />
      )}
      {confirm?.kind === "revoke-all" && (
        <ConfirmDialog
          title="撤销全部分享"
          message="确定撤销你的全部有效分享？该操作影响所有访问者，且不可恢复。"
          confirmLabel="全部撤销"
          danger
          onConfirm={() => void handleRevokeAll()}
          onCancel={() => setConfirm(null)}
        />
      )}
    </section>
  );
}
