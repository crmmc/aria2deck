import { DialogShell } from "@/components/ui/DialogShell";

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
        >
          ×
        </button>
      </div>

      <p className="muted text-sm mb-3">
        每行输入一个链接，最多 30 个；空行和重复链接将被忽略
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
    </DialogShell>
  );
}
