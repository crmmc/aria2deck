"use client";

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { ShareLink } from "@/types";
import { useToast } from "@/components/Toast";
import { formatBytes } from "@/lib/utils";

interface ShareCardProps {
  record: ShareLink;
  isSelected: boolean;
  onToggleSelection: (id: number) => void;
  onCopyLink: (shareCode: string) => void;
  onRevoke: (id: number) => void;
  onDelete: (id: number) => void;
}

const ShareCard = memo(function ShareCard({
  record,
  isSelected,
  onToggleSelection,
  onCopyLink,
  onRevoke,
  onDelete,
}: ShareCardProps) {
  const handleCardClick = useCallback(() => {
    onCopyLink(record.share_code);
  }, [record.share_code, onCopyLink]);

  const handleCheckboxChange = useCallback(() => {
    onToggleSelection(record.id);
  }, [record.id, onToggleSelection]);

  const handleCopyClick = useCallback(() => {
    onCopyLink(record.share_code);
  }, [record.share_code, onCopyLink]);

  const handleRevokeClick = useCallback(() => {
    onRevoke(record.id);
  }, [record.id, onRevoke]);

  const handleDeleteClick = useCallback(() => {
    onDelete(record.id);
  }, [record.id, onDelete]);

  const isExpiredByTime = record.expires_at ? new Date(record.expires_at) <= new Date() : false;
  const isExpiredByCount = record.max_downloads != null && record.max_downloads > 0 && record.download_count >= record.max_downloads;
  const isExpired = isExpiredByTime || isExpiredByCount;
  
  let currentStatus: string = record.status;
  if (currentStatus === "active" && isExpired) {
    currentStatus = "expired";
  }

  const statusText =
    currentStatus === "active"
      ? "活跃"
      : currentStatus === "expired"
        ? "已过期"
        : "已失效";

  const statusClass =
    currentStatus === "active"
      ? "task-status-complete"
      : currentStatus === "expired"
        ? "task-status-cancelled"
        : "task-status-error";

  return (
    <div
      className="card cursor-pointer"
      onClick={handleCardClick}
    >
      <div className={`task-card-inner${isSelected ? " selected" : ""}`}>
        <div>
          <div className="space-between flex-start mb-3">
            <div className="task-card-header">
              <input
                type="checkbox"
                checked={isSelected}
                onChange={handleCheckboxChange}
                onClick={(e) => e.stopPropagation()}
                className="checkbox-sm mt-2 cursor-pointer"
              />
              <div className="overflow-hidden flex-1">
                <h3 className="task-name" title={record.file_name}>
                  {record.file_name} {record.has_password && <span className="muted text-sm ml-1" title="有密码">🔒</span>}
                </h3>
                <div className="muted tabular-nums text-sm">
                  {formatBytes(record.file_size)}
                </div>
              </div>
            </div>
            <span
              className={`task-status ${statusClass}`}
              style={{ marginLeft: "auto" }}
            >
              {statusText}
            </span>
          </div>

          <div className="text-sm mb-3 muted">
            <div>提取码: {record.share_code}</div>
            <div>
              下载次数: {record.download_count}
              {record.max_downloads != null && record.max_downloads > 0 ? ` / ${record.max_downloads}` : ""}
            </div>
            {record.expires_at && (
              <div>
                过期时间: {new Date(record.expires_at).toLocaleString()}
              </div>
            )}
          </div>
        </div>

        <div className="task-card-footer" onClick={(e) => e.stopPropagation()}>
          <div className="task-footer-left">
            <span className="muted text-sm">
              创建于 {new Date(record.created_at).toLocaleString()}
            </span>
          </div>

          <div className="task-footer-right">
            <button
              className="button secondary btn-sm"
              onClick={handleCopyClick}
              title="复制链接"
            >
              复制链接
            </button>
            {currentStatus === "active" && (
              <button
                className="button secondary btn-sm"
                onClick={handleRevokeClick}
                title="失效"
              >
                失效
              </button>
            )}
            <button
              className="button secondary danger btn-sm"
              onClick={handleDeleteClick}
              title="删除"
            >
              删除
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});

export default function SharesPage() {
  const { showToast, showConfirm } = useToast();
  const [records, setRecords] = useState<ShareLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedRecords, setSelectedRecords] = useState<Set<number>>(
    new Set()
  );
  const [filterStatus, setFilterStatus] = useState<string>("all");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [isOperating, setIsOperating] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    loadShares();
    return () => { mountedRef.current = false; };
  }, []);

  async function loadShares() {
    setLoading(true);
    try {
      const shares = await api.listShares();
      if (!mountedRef.current) return;
      setRecords(shares);
    } catch {
      if (!mountedRef.current) return;
      showToast("加载分享记录失败", "error");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }

  const copyLink = useCallback(
    (shareCode: string) => {
      const link = `${window.location.origin}/s/${shareCode}`;
      navigator.clipboard
        .writeText(link)
        .then(() => {
          showToast("链接已复制", "success");
        })
        .catch(() => {
          showToast("复制失败", "error");
        });
    },
    [showToast]
  );

  const revokeShare = useCallback(
    async (id: number) => {
      try {
        await api.revokeShare(id);
        showToast("分享已失效", "success");
        loadShares();
      } catch (err) {
        showToast("操作失败：" + (err as Error).message, "error");
      }
    },
    [showToast]
  );

  const deleteShare = useCallback(
    async (id: number) => {
      const confirmed = await showConfirm({
        title: "删除分享",
        message: "确定要删除这条分享记录吗？",
        confirmText: "删除",
        danger: true,
      });
      if (!confirmed) return;

      try {
        await api.deleteShare(id);
        showToast("分享已删除", "success");
        loadShares();
      } catch (err) {
        showToast("删除失败：" + (err as Error).message, "error");
      }
    },
    [showToast, showConfirm]
  );

  async function revokeAllShares() {
    if (records.length === 0) {
      showToast("没有分享记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "一键失效全部",
      message: "确定要让所有活跃的分享链接失效吗？此操作不可恢复。",
      confirmText: "确定失效",
      danger: true,
    });
    if (!confirmed) return;

    setIsOperating(true);
    try {
      await api.revokeAllShares();
      showToast("已让所有分享失效", "success");
      loadShares();
    } catch (err) {
      showToast("操作失败：" + (err as Error).message, "error");
    } finally {
      setIsOperating(false);
    }
  }

  const toggleRecordSelection = useCallback((id: number) => {
    setSelectedRecords((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const filteredRecords = useMemo(() => {
    let filtered = records;

    if (searchKeyword.trim()) {
      const keyword = searchKeyword.toLowerCase();
      filtered = filtered.filter((r) =>
        r.file_name.toLowerCase().includes(keyword) || r.share_code.toLowerCase().includes(keyword)
      );
    }

    if (filterStatus !== "all") {
      filtered = filtered.filter((r) => {
        const isExpiredByTime = r.expires_at ? new Date(r.expires_at) <= new Date() : false;
        const isExpiredByCount = r.max_downloads != null && r.max_downloads > 0 && r.download_count >= r.max_downloads;
        const isExpired = isExpiredByTime || isExpiredByCount;
        
        let currentStatus: string = r.status;
        if (currentStatus === "active" && isExpired) {
          currentStatus = "expired";
        }
        
        return currentStatus === filterStatus;
      });
    }

    return filtered;
  }, [records, searchKeyword, filterStatus]);

  const toggleSelectAll = useCallback(() => {
    if (selectedRecords.size === filteredRecords.length) {
      setSelectedRecords(new Set());
    } else {
      setSelectedRecords(new Set(filteredRecords.map((r) => r.id)));
    }
  }, [selectedRecords.size, filteredRecords]);

  async function batchDeleteShares() {
    const selectedList = records.filter((r) => selectedRecords.has(r.id));
    if (selectedList.length === 0) {
      showToast("请先选择要删除的记录", "warning");
      return;
    }

    const confirmed = await showConfirm({
      title: "删除分享",
      message: `确定要删除选中的 ${selectedList.length} 条分享记录吗？`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;

    setIsOperating(true);
    try {
      await Promise.all(selectedList.map((r) => api.deleteShare(r.id)));
      setSelectedRecords(new Set());
      showToast(`已删除 ${selectedList.length} 条分享记录`, "success");
      loadShares();
    } catch (err) {
      showToast("删除失败：" + (err as Error).message, "error");
    } finally {
      setIsOperating(false);
    }
  }

  return (
    <div className="glass-frame full-height animate-in">
      <div className="space-between mb-7">
        <div>
          <h1 className="text-2xl">分享管理</h1>
          <p className="muted">管理你的文件分享链接</p>
        </div>
      </div>

      <div className="card filter-toolbar">
        <div className="filter-group">
          <input
            type="text"
            placeholder="搜索分享..."
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
            className="search-input"
          />
        </div>

        <div className="filter-group">
          <span className="muted text-sm">筛选:</span>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="select"
          >
            <option value="all">全部</option>
            <option value="active">活跃</option>
            <option value="expired">已过期</option>
            <option value="revoked">已失效</option>
          </select>
        </div>

        <div className="filter-group ml-auto">
          {selectedRecords.size > 0 && (
            <>
              <span className="muted text-sm">
                已选 {selectedRecords.size} 项
              </span>
              <button
                type="button"
                className={`button secondary danger btn-sm${isOperating ? " opacity-60" : ""}`}
                onClick={batchDeleteShares}
                disabled={isOperating}
              >
                删除选中
              </button>
            </>
          )}
          {records.length > 0 && (
            <button
              type="button"
              className={`button secondary btn-sm${isOperating ? " opacity-60" : ""}`}
              onClick={revokeAllShares}
              disabled={isOperating}
            >
              一键失效全部
            </button>
          )}
          <button
            type="button"
            className="button secondary btn-sm"
            onClick={toggleSelectAll}
          >
            {selectedRecords.size === filteredRecords.length &&
            filteredRecords.length > 0
              ? "取消全选"
              : "全选"}
          </button>
        </div>
      </div>

      <div className="task-list">
        {loading ? (
          <div className="empty-state">
            <p className="muted">加载中...</p>
          </div>
        ) : filteredRecords.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-icon">
              <svg
                width="48"
                height="48"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="18" cy="5" r="3" />
                <circle cx="6" cy="12" r="3" />
                <circle cx="18" cy="19" r="3" />
                <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
                <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
              </svg>
            </div>
            <p className="font-medium mb-1">暂无分享记录</p>
            <p className="muted text-base">你创建的文件分享链接将显示在这里</p>
          </div>
        ) : (
          filteredRecords.map((record) => (
            <ShareCard
              key={record.id}
              record={record}
              isSelected={selectedRecords.has(record.id)}
              onToggleSelection={toggleRecordSelection}
              onCopyLink={copyLink}
              onRevoke={revokeShare}
              onDelete={deleteShare}
            />
          ))
        )}
      </div>
    </div>
  );
}
