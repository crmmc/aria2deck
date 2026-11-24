"use client";

import { useMemo, useRef, useState } from "react";

import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { DialogShell } from "@/shared/components/DialogShell";
import { requestModalClose } from "@/shared/ui/modalClose";
import { createTorrent } from "../api";
import type { TaskResponse, TorrentPreviewResponse } from "@/shared/api/types";
import { buildTorrentTree, fillDirectorySizes, type TorrentTreeNode } from "../model";

import { TorrentFileTree } from "./TorrentFileTree";
import { TorrentSelectionSummary } from "./TorrentSelectionSummary";

type TorrentCreateWizardProps = {
  torrentBase64: string;
  preview: TorrentPreviewResponse;
  onCancel: () => void;
  onCreated: (task: TaskResponse) => void;
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
  const [stage, setStage] = useState<Stage>("select");
  const [searchQuery, setSearchQuery] = useState("");
  const [showCancelConfirm, setShowCancelConfirm] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const wizardRef = useRef<HTMLDivElement>(null);
  const confirmOuterCloseRef = useRef(false);
  // 归档参考 交互：初始全选；树按插入顺序构建（种子树顺序是视觉基线）。
  const [selectedIndexes, setSelectedIndexes] = useState<Set<number>>(
    () => new Set(preview.files.map((file) => file.index)),
  );

  const tree: TorrentTreeNode[] = useMemo(
    () => fillDirectorySizes(buildTorrentTree(preview.files)),
    [preview.files],
  );
  const totalCount = preview.files.length;

  const selectedFiles = useMemo(
    () => preview.files.filter((file) => selectedIndexes.has(file.index)),
    [preview.files, selectedIndexes],
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

  const create = async (trigger: Element) => {
    if (isCreating || selectedIndexes.size === 0) return;
    setIsCreating(true);
    setSubmitError(null);
    try {
      const task = await createTorrent(
        torrentBase64,
        Array.from(selectedIndexes).sort((a, b) => a - b),
      );
      onCreated(task);
      requestModalClose(trigger);
    } catch (err) {
      // 向导是全屏遮罩：错误必须显示在向导内，页面级 error 会被挡住。
      const message = err instanceof Error ? err.message : "创建失败";
      setSubmitError(message);
      onError(message);
      setIsCreating(false);
    }
  };

  const closeCancelConfirm = () => {
    setShowCancelConfirm(false);
    if (!confirmOuterCloseRef.current) return;
    confirmOuterCloseRef.current = false;
    requestModalClose(wizardRef.current);
  };

  return (
    <ModalOverlay
      onClose={onCancel}
      onRequestClose={() => setShowCancelConfirm(true)}
      closeDisabled={isCreating}
      ariaLabel="添加 BT 下载任务"
      className="modal-overlay torrent-wizard-backdrop"
      contentClassName="torrent-wizard"
    >
      <header className="torrent-wizard-header">
          <div>
            <h2>添加 BT 下载任务</h2>
          </div>
        </header>
        <div ref={wizardRef} className="torrent-wizard-shell">
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
                totalCount={totalCount}
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
                nodes={tree}
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
                nodes={tree}
                selectedIndexes={selectedIndexes}
                searchQuery=""
                readonly
              />
              <footer className="torrent-wizard-actions">
                {submitError ? (
                  <div
                    className="form-error torrent-submit-error"
                    role="alert"
                    style={{ flex: "1 1 100%", marginBottom: "0.5rem" }}
                  >
                    {submitError}
                  </div>
                ) : null}
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
                  onClick={(event) => void create(event.currentTarget)}
                >
                  {isCreating ? "提交中" : "确认"}
                </button>
              </footer>
            </main>
          )}
        </div>

      {showCancelConfirm ? (
        <DialogShell
          onClose={closeCancelConfirm}
          ariaLabel="取消添加任务"
          className="modal-overlay torrent-cancel-layer"
          contentClassName="torrent-cancel-dialog"
        >
          <h3>取消添加任务？</h3>
          <p>当前文件选择不会保存，取消后需要重新上传种子。</p>
          <div className="torrent-cancel-actions">
            <button
              type="button"
              className="button secondary shadow-none"
              onClick={(event) => requestModalClose(event.currentTarget)}
            >
              继续选择
            </button>
            <button
              type="button"
              className="button shadow-none"
              onClick={(event) => {
                confirmOuterCloseRef.current = true;
                requestModalClose(event.currentTarget);
              }}
            >
              确认取消
            </button>
          </div>
        </DialogShell>
      ) : null}
    </ModalOverlay>
  );
}
