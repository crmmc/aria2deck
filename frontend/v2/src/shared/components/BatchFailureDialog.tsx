"use client";

import { DialogShell } from "@/shared/components/DialogShell";
import { Icon } from "@/shared/components/Icon";
import { requestModalClose } from "@/shared/ui/modalClose";

export type BatchFailure = {
  label: string;
  reason: string;
};

type BatchFailureDialogProps = {
  failures: BatchFailure[];
  onClose: () => void;
};

/** 批量操作的失败明细：成功项走 toast，失败项逐条列出对象名与原因。 */
export function BatchFailureDialog({ failures, onClose }: BatchFailureDialogProps) {
  if (failures.length === 0) return null;
  return (
    <DialogShell
      onClose={onClose}
      ariaLabel="批量操作失败明细"
      contentClassName="modal-content batch-failure-content"
    >
      <div className="dialog-shell">
        <div className="dialog-head">
          <h2>未完成的 {failures.length} 项</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="关闭对话框"
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            <Icon name="x" />
          </button>
        </div>
        <ul className="batch-failure-list">
          {failures.map((item, index) => (
            <li key={`${item.label}-${index}`}>
              <strong title={item.label}>{item.label}</strong>
              <span>{item.reason}</span>
            </li>
          ))}
        </ul>
        <div className="batch-failure-actions">
          <button
            type="button"
            className="button"
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            知道了
          </button>
        </div>
      </div>
    </DialogShell>
  );
}
