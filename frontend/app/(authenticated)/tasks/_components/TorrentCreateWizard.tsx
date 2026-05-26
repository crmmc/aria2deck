"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { Task, TorrentPreview } from "@/types";

import { TorrentFileTree } from "./TorrentFileTree";
import { TorrentSelectionSummary } from "./TorrentSelectionSummary";

type TorrentCreateWizardProps = {
  torrentBase64: string;
  preview: TorrentPreview;
  onCancel: () => void;
  onCreated: (task: Task) => void;
  onError: (message: string) => void;
};

type Stage = "select" | "confirm";

export function TorrentCreateWizard({
  torrentBase64,
  preview,
  onCancel,
  onCreated,
  onError,
}: TorrentCreateWizardProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const [stage, setStage] = useState<Stage>("select");
  const [searchQuery, setSearchQuery] = useState("");
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);
  const [isCreating, setIsCreating] = useState(false);
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(
    () => new Set(preview.files.map((file) => file.index))
  );

  const selectedFiles = useMemo(
    () => preview.files.filter((file) => selectedIndexes.has(file.index)),
    [preview.files, selectedIndexes]
  );
  const selectedSize = selectedFiles.reduce((sum, file) => sum + file.size, 0);

  const toggleFile = (index: number) => {
    setSelectedIndexes((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const toggleDirectory = (indexes: number[]) => {
    setSelectedIndexes((current) => {
      const next = new Set(current);
      const allSelected = indexes.every((index) => next.has(index));
      indexes.forEach((index) => {
        if (allSelected) next.delete(index);
        else next.add(index);
      });
      return next;
    });
  };

  const selectAll = () => {
    setSelectedIndexes(new Set(preview.files.map((file) => file.index)));
  };

  const clearAll = () => {
    setSelectedIndexes(new Set());
  };

  const createTorrent = async () => {
    if (isCreating || selectedIndexes.size === 0) return;
    setIsCreating(true);
    try {
      const task = await api.uploadTorrent(torrentBase64, {
        selected_file_indexes: Array.from(selectedIndexes).sort((a, b) => a - b),
      });
      onCreated(task);
    } catch (err) {
      onError((err as Error).message);
      setIsCreating(false);
    }
  };

  return (
    <div className="torrent-wizard-backdrop" role="presentation">
      <section
        ref={dialogRef}
        className="torrent-wizard"
        role="dialog"
        aria-modal="true"
        aria-label="添加 BT 下载任务"
        tabIndex={-1}
      >
        <header className="torrent-wizard-header">
          <div>
            <h2>添加 BT 下载任务</h2>
          </div>
        </header>
        <div className="torrent-wizard-shell">
          <aside className="torrent-stage-tabs">
            <button type="button" className="torrent-stage-tab torrent-stage-tab-done">
              <span>1</span>
              <strong>上传解析</strong>
            </button>
            <button
              type="button"
              className={`torrent-stage-tab ${
                stage === "select" ? "torrent-stage-tab-active" : "torrent-stage-tab-done"
              }`}
              onClick={() => !isCreating && setStage("select")}
            >
              <span>2</span>
              <strong>选择文件</strong>
            </button>
            <button
              type="button"
              className={`torrent-stage-tab ${stage === "confirm" ? "torrent-stage-tab-active" : ""}`}
              onClick={() => selectedIndexes.size > 0 && !isCreating && setStage("confirm")}
            >
              <span>3</span>
              <strong>确认下载</strong>
            </button>
          </aside>

          {stage === "select" ? (
            <main className="torrent-wizard-panel torrent-wizard-panel-select">
              <TorrentSelectionSummary
                selectedCount={selectedIndexes.size}
                selectedSize={selectedSize}
                totalCount={preview.file_count}
              />
              <div className="torrent-toolbar">
                <input
                  id="torrent-search"
                  className="input torrent-search"
                  placeholder="搜索文件"
                  aria-label="搜索文件"
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                />
                <div className="torrent-toolbar-actions">
                  <button type="button" className="button secondary shadow-none" onClick={selectAll}>
                    全选
                  </button>
                  <button type="button" className="button secondary shadow-none" onClick={clearAll}>
                    清空
                  </button>
                </div>
              </div>
              <TorrentFileTree
                nodes={preview.tree}
                selectedIndexes={selectedIndexes}
                searchQuery={searchQuery}
                onToggleFile={toggleFile}
                onToggleDirectory={toggleDirectory}
              />
              <footer className="torrent-wizard-actions">
                <button
                  type="button"
                  className="button secondary shadow-none"
                  onClick={() => setShowCancelConfirm(true)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button shadow-none"
                  disabled={selectedIndexes.size === 0}
                  onClick={() => setStage("confirm")}
                >
                  下一阶段
                </button>
              </footer>
            </main>
          ) : (
            <main className="torrent-wizard-panel torrent-wizard-panel-confirm">
              <div className="torrent-review-head">
                <h3>确认下载内容</h3>
                <p>{selectedIndexes.size} 个文件</p>
              </div>
              <TorrentFileTree
                nodes={preview.tree}
                selectedIndexes={selectedIndexes}
                searchQuery=""
                readonly
              />
              <footer className="torrent-wizard-actions">
                <button
                  type="button"
                  className="button secondary shadow-none"
                  disabled={isCreating}
                  onClick={() => setStage("select")}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="button shadow-none"
                  disabled={isCreating}
                  onClick={createTorrent}
                >
                  {isCreating ? "提交中" : "确认"}
                </button>
              </footer>
            </main>
          )}
        </div>

        {showCancelConfirm ? (
          <div className="torrent-cancel-layer" role="presentation">
            <section
              className="torrent-cancel-dialog"
              role="dialog"
              aria-modal="true"
              aria-label="取消添加任务"
            >
              <h3>取消添加任务？</h3>
              <p>当前文件选择不会保存，取消后需要重新上传种子。</p>
              <div className="torrent-cancel-actions">
                <button
                  type="button"
                  className="button secondary shadow-none"
                  onClick={() => setShowCancelConfirm(false)}
                >
                  继续选择
                </button>
                <button type="button" className="button shadow-none" onClick={onCancel}>
                  确认取消
                </button>
              </div>
            </section>
          </div>
        ) : null}
      </section>
    </div>
  );
}
