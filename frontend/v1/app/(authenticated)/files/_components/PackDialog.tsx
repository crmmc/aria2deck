import { ModalOverlay } from "@/components/ModalOverlay";
import { formatBytes } from "@/lib/utils";

type PackDialogProps = {
  selectedCount: number;
  packSize: number | null;
  availableSpace: number | null;
  packOutputName: string;
  packDeleteSource: boolean;
  packLoading: boolean;
  packing: boolean;
  onClose: () => void;
  onOutputNameChange: (value: string) => void;
  onDeleteSourceChange: (value: boolean) => void;
  onConfirm: () => void;
};

export function PackDialog({
  selectedCount,
  packSize,
  availableSpace,
  packOutputName,
  packDeleteSource,
  packLoading,
  packing,
  onClose,
  onOutputNameChange,
  onDeleteSourceChange,
  onConfirm,
}: PackDialogProps) {
  const hasInsufficientSpace =
    packSize !== null && availableSpace !== null && packSize > availableSpace;

  return (
    <ModalOverlay
      onClose={() => !packing && onClose()}
      ariaLabel="创建打包任务"
      contentClassName="batch-modal-content"
      contentStyle={{ maxWidth: "500px", width: "90%" }}
    >
      <div className="modal-header">
        <h2 className="m-0">打包</h2>
        <button
          type="button"
          onClick={() => !packing && onClose()}
          className="modal-close-btn"
          aria-label="关闭打包弹窗"
          disabled={packing}
        >
          ×
        </button>
      </div>

      {packLoading ? (
        <div className="text-center py-8">
          <p className="muted">计算中...</p>
        </div>
      ) : (
        <div className="p-4">
          <div className="mb-4">
            <p className="text-base mb-2">
              已选择 <strong>{selectedCount}</strong> 个文件
            </p>
            {packSize !== null && (
              <p className="text-base mb-2">
                预估大小: <strong>{formatBytes(packSize)}</strong>
              </p>
            )}
            {availableSpace !== null && (
              <p className="text-base mb-2">
                可用空间: <strong>{formatBytes(availableSpace)}</strong>
              </p>
            )}
            {hasInsufficientSpace && (
              <p className="text-danger text-sm">
                空间不足，无法创建打包任务
              </p>
            )}
          </div>

          <div className="mb-4">
            <label className="text-sm muted mb-1 block" htmlFor="pack-output-name">
              输出文件名 (可选)
            </label>
            <input
              id="pack-output-name"
              type="text"
              className="input"
              placeholder="默认自动生成"
              value={packOutputName}
              onChange={(event) => onOutputNameChange(event.target.value)}
              maxLength={200}
              disabled={packing}
              aria-label="输出文件名"
            />
          </div>

          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={packDeleteSource}
              onChange={(event) => onDeleteSourceChange(event.target.checked)}
              disabled={packing}
              aria-label="打包后删除源文件"
            />
            <span>打包后删除源文件</span>
          </label>

          <div className="flex gap-3 flex-end">
            <button
              type="button"
              className="button secondary"
              onClick={onClose}
              disabled={packing}
            >
              取消
            </button>
            <button
              type="button"
              className="button primary"
              onClick={onConfirm}
              disabled={packing || hasInsufficientSpace}
            >
              {packing ? "创建中..." : "确认打包"}
            </button>
          </div>
        </div>
      )}
    </ModalOverlay>
  );
}
