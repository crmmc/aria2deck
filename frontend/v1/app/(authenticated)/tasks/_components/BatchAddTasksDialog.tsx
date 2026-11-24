import { useEffect, useRef } from "react";

import { DialogShell } from "@/components/ui/DialogShell";

export type BatchFailure = { uri: string; reason: string };
export type BatchFeedback =
  | { kind: "result"; acceptedCount: number; failures: BatchFailure[] }
  | { kind: "error"; message: string };

type BatchAddTasksDialogProps = {
  batchUris: string;
  isBatchAdding: boolean;
  feedback: BatchFeedback | null;
  onBatchUrisChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onRetryFailed: () => void;
};

export function BatchAddTasksDialog({
  batchUris,
  isBatchAdding,
  feedback,
  onBatchUrisChange,
  onSubmit,
  onCancel,
  onRetryFailed,
}: BatchAddTasksDialogProps) {
  const showResult = feedback?.kind === "result";
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const previousShowResultRef = useRef(showResult);

  // 两个视图互相替换掉持有焦点的元素，切换时必须接管焦点，否则焦点回落 <body>。
  // 首次挂载不动焦点，保留 ModalOverlay 的初始聚焦行为。
  useEffect(() => {
    if (previousShowResultRef.current === showResult) return;
    previousShowResultRef.current = showResult;
    if (showResult) {
      retryButtonRef.current?.focus();
    } else {
      textareaRef.current?.focus();
    }
  }, [showResult]);

  return (
    <DialogShell
      onClose={onCancel}
      ariaLabel="批量添加任务"
      contentClassName="batch-modal-content"
    >
      <div className="modal-header">
        <h2 className="m-0">批量添加任务</h2>
        <button
          type="button"
          onClick={onCancel}
          className="modal-close-btn"
          aria-label="关闭批量添加任务"
          disabled={isBatchAdding}
        >
          ×
        </button>
      </div>

      {feedback?.kind === "error" ? (
        <div className="form-error" role="alert">
          {feedback.message}
        </div>
      ) : null}

      {feedback?.kind === "result" ? (
        <>
          <p className="text-sm mb-3" role="status">
            已提交 {feedback.acceptedCount} 个，{feedback.failures.length} 个失败
          </p>

          <ul className="flex-1 overflow-auto flex flex-col gap-3 p-0 m-0">
            {feedback.failures.map((failure, index) => (
              <li
                key={`${failure.uri}-${index}`}
                className="flex flex-col gap-1"
              >
                <span className="text-sm font-mono break-all">{failure.uri}</span>
                <span className="text-xs text-danger break-all">
                  {failure.reason}
                </span>
              </li>
            ))}
          </ul>

          <div className="modal-footer mt-4">
            <button
              type="button"
              ref={retryButtonRef}
              className="button secondary btn-task"
              onClick={onRetryFailed}
            >
              重试失败项
            </button>
            <button type="button" className="button btn-task" onClick={onCancel}>
              完成
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="muted text-sm mb-3">
            每行输入一个链接，最多 30 个；空行和重复链接将被忽略
          </p>

          <textarea
            ref={textareaRef}
            value={batchUris}
            onChange={(event) => onBatchUrisChange(event.target.value)}
            placeholder="magnet:?xt=urn:btih:...&#10;https://example.com/file1.zip&#10;https://example.com/file2.zip"
            className="batch-textarea"
            aria-label="批量下载链接"
          />

          <div className="modal-footer">
            <button
              type="button"
              className="button secondary btn-task"
              onClick={onCancel}
              disabled={isBatchAdding}
            >
              取消
            </button>
            <button
              type="button"
              className="button btn-task"
              onClick={onSubmit}
              disabled={isBatchAdding}
            >
              {isBatchAdding ? "添加中..." : "添加任务"}
            </button>
          </div>
        </>
      )}
    </DialogShell>
  );
}
