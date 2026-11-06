import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth, useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  Pagination,
} from "@/shared/components/ui";
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue";
import type {
  StorageFileResponse,
  StorageReferencingUserResponse,
} from "@/shared/api/types";
import { formatBytes, formatDateTime } from "@/shared/lib/format";
import { deleteStorageFiles, fetchReferencingUsers, fetchStorageFiles } from "./api";

const PAGE_SIZE = 20;

const DELETION_STATE_LABEL: Record<StorageFileResponse["deletion_state"], string> = {
  active: "正常",
  pending_delete: "待删除",
  tombstoned: "已墓碑",
};

/** R3 哈希列：内容键形如 "file:<sha256>"，取摘要段截断展示、悬停全文。 */
function digestOf(contentKey: string): string {
  const separator = contentKey.indexOf(":");
  return separator >= 0 ? contentKey.slice(separator + 1) : contentKey;
}

function shortDigest(contentKey: string): string {
  const digest = digestOf(contentKey);
  return digest.length > 8 ? `${digest.slice(0, 8)}…` : digest;
}

export function StoragePage() {
  const { user: me } = useAuth();
  const { push } = useToast();
  const [items, setItems] = useState<StorageFileResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search, 300);
  const [orphansOnly, setOrphansOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [referencing, setReferencing] = useState<StorageFileResponse | null>(null);
  const [refUsers, setRefUsers] = useState<StorageReferencingUserResponse[] | null>(null);
  const [refError, setRefError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number[] | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchStorageFiles(page, PAGE_SIZE, debouncedSearch, orphansOnly);
      if (seq !== requestSeq.current) return; // 丢弃过期响应。
      setItems(response.items);
      setTotal(response.total);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(caught, "存储列表加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [page, debouncedSearch, orphansOnly]);

  useEffect(() => {
    if (me === null || !me.is_admin) return; // 非管理员跳离，不发列表请求。
    void load();
  }, [load, me]);

  if (me !== null && !me.is_admin) {
    return <Navigate to="/tasks" replace />;
  }

  const changePage = (next: number) => {
    setPage(next);
    setSelected(new Set()); // 翻页清空选择。
  };

  const openReferencing = async (target: StorageFileResponse) => {
    setReferencing(target);
    setRefUsers(null);
    setRefError(null);
    try {
      const response = await fetchReferencingUsers(target.content_id);
      setRefUsers(response.items);
    } catch (caught) {
      setRefError(errorMessage(caught, "引用用户加载失败"));
    }
  };

  const handleDelete = async () => {
    if (confirmDelete === null) return;
    const ids = confirmDelete;
    setConfirmDelete(null);
    try {
      const response = await deleteStorageFiles(ids);
      if (response.rejected_count > 0) {
        const firstRejected = response.results.find((result) => !result.accepted);
        const reason = firstRejected?.error?.message ?? "部分内容不可删除";
        push(
          "error",
          `存储删除完成：已受理 ${response.accepted_count} 个，拒绝 ${response.rejected_count} 个（${reason}）`,
        );
      } else {
        push("success", `存储删除已受理 ${response.accepted_count} 个，后台清理进行中`);
      }
      const maxPage = Math.max(1, Math.ceil((total - response.accepted_count) / PAGE_SIZE));
      if (page > maxPage) setPage(maxPage);
      else await load();
      setSelected(new Set());
    } catch (caught) {
      push("error", errorMessage(caught, "存储删除失败"));
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
    <section className="page" aria-label="存储管理">
      <header className="page-header">
        <h1>存储</h1>
        <div className="header-actions">
          <button
            type="button"
            className="secondary-button"
            disabled={loading}
            onClick={() => void load()}
          >
            刷新
          </button>
          <label className="search-field">
            <span className="visually-hidden">搜索存储文件</span>
            <input
              type="search"
              value={search}
              onChange={(event) => {
                // 条件变化与页码重置/清选择在同一事件内，避免旧页码双请求。
                setSearch(event.target.value);
                setPage(1);
                setSelected(new Set());
              }}
              placeholder="搜索内容键或路径…"
              aria-label="搜索存储文件"
            />
          </label>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={orphansOnly}
              onChange={(event) => {
                setOrphansOnly(event.target.checked);
                setPage(1);
                setSelected(new Set());
              }}
              aria-label="仅显示孤儿文件"
            />
            <span>仅孤儿</span>
          </label>
          {selected.size > 0 && (
            <button
              type="button"
              className="danger-button"
              onClick={() => setConfirmDelete([...selected])}
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
          title="暂无存储文件"
          hint={orphansOnly ? "当前筛选下没有孤儿内容。" : "下载完成的内容会出现在这里。"}
        />
      ) : (
        <>
          <table className="data-table" aria-label="存储文件列表">
            <thead>
              <tr>
                <th scope="col" className="select-col">
                  <input
                    type="checkbox"
                    aria-label="全选存储文件"
                    checked={items.length > 0 && items.every((item) => selected.has(item.content_id))}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? new Set(items.map((item) => item.content_id))
                          : new Set(),
                      )
                    }
                  />
                </th>
                <th scope="col">内容键</th>
                <th scope="col">文件名</th>
                <th scope="col">哈希</th>
                <th scope="col">类型</th>
                <th scope="col">大小</th>
                <th scope="col">创建时间</th>
                <th scope="col">磁盘</th>
                <th scope="col">引用数</th>
                <th scope="col">删除状态</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.content_id} data-testid={`storage-row-${item.content_id}`}>
                  <td className="select-col">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${item.content_key}`}
                      checked={selected.has(item.content_id)}
                      onChange={() => toggle(item.content_id)}
                    />
                  </td>
                  <td className="name-cell" title={item.storage_path}>
                    {item.content_key}
                  </td>
                  <td className="name-cell" title={item.file_names.join("、")}>
                    {item.file_names.length > 0 ? item.file_names.join("、") : "—"}
                  </td>
                  <td title={digestOf(item.content_key)}>{shortDigest(item.content_key)}</td>
                  <td>{item.kind === "directory" ? "目录" : "文件"}</td>
                  <td>{formatBytes(item.size)}</td>
                  <td>{formatDateTime(item.created_at_ms)}</td>
                  <td>
                    <span className={`badge tone-${item.on_disk ? "ok" : "error"}`}>
                      {item.on_disk ? "在磁盘" : "缺失"}
                    </span>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => void openReferencing(item)}
                    >
                      {item.reference_count} 个引用
                    </button>
                  </td>
                  <td>
                    <span
                      className={`badge tone-${
                        item.deletion_state === "active" ? "muted" : "warn"
                      }`}
                    >
                      {DELETION_STATE_LABEL[item.deletion_state]}
                    </span>
                  </td>
                  <td className="actions-cell">
                    <button
                      type="button"
                      className="link-button"
                      disabled={item.reference_count > 0}
                      title={
                        item.reference_count > 0 ? "仍有引用的内容不可删除，请先查看引用用户" : undefined
                      }
                      onClick={() => setConfirmDelete([item.content_id])}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={changePage} />
        </>
      )}

      {referencing !== null && (
        <Modal
          title={`引用用户：${referencing.content_key}`}
          onClose={() => setReferencing(null)}
        >
          {refError !== null ? (
            <ErrorState message={refError} onRetry={() => void openReferencing(referencing)} />
          ) : refUsers === null ? (
            <LoadingState />
          ) : refUsers.length === 0 ? (
            <EmptyState title="无引用用户" hint="该内容当前是孤儿，可安全删除。" />
          ) : (
            <table className="data-table" aria-label="引用用户列表">
              <thead>
                <tr>
                  <th scope="col">用户名</th>
                  <th scope="col">显示名</th>
                  <th scope="col">引用时间</th>
                </tr>
              </thead>
              <tbody>
                {refUsers.map((entry) => (
                  <tr key={entry.user_id}>
                    <td>{entry.username}</td>
                    <td>{entry.display_name}</td>
                    <td>{formatDateTime(entry.referenced_at_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Modal>
      )}

      {confirmDelete !== null && (
        <ConfirmDialog
          title="删除存储文件"
          message={`确定删除选中的 ${confirmDelete.length} 个存储内容？仅无引用的孤儿内容会被受理，受理后由后台执行物理清理，不可恢复。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleDelete()}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </section>
  );
}
