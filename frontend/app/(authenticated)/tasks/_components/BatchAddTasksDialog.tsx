import { ModalOverlay } from "@/components/ModalOverlay";

type BatchAddTasksDialogProps = {
  batchUris: string;
  isBatchAdding: boolean;
  onBatchUrisChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

export function BatchAddTasksDialog({
  batchUris,
  isBatchAdding,
  onBatchUrisChange,
  onSubmit,
  onCancel,
}: BatchAddTasksDialogProps) {
  return (
    <ModalOverlay
      onClose={onCancel}
      contentClassName="batch-modal-content"
    >
      <div className="modal-header">
        <h2 className="m-0">批量添加任务</h2>
        <button
          type="button"
          onClick={onCancel}
          className="modal-close-btn"
        >
          ×
        </button>
      </div>

      <p className="muted text-sm mb-3">
        每行输入一个链接，支持磁力链接、HTTP 或 FTP URL
      </p>

      <textarea
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
    </ModalOverlay>
  );
}
