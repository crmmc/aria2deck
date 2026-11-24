import { useEffect, useRef } from "react";

import { DialogShell } from "@/shared/components/DialogShell";
import { Icon } from "@/shared/components/Icon";
import { requestModalClose } from "@/shared/ui/modalClose";
import type { UserResponse } from "@/shared/api/types";

type DeleteUserDialogProps = {
  user: UserResponse;
  deleting: boolean;
  onConfirm: (trigger: Element) => void;
  onClose: () => void;
};

export function DeleteUserDialog({ user, deleting, onConfirm, onClose }: DeleteUserDialogProps) {
  const returnButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    returnButtonRef.current?.focus();
  }, []);

  return (
    <DialogShell
      onClose={onClose}
      ariaLabel="删除用户"
      contentClassName="modal-content admin-dialog-content"
      closeDisabled={deleting}
    >
      <div className="dialog-shell">
        <div className="dialog-head">
          <h2>删除用户</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="关闭对话框"
            disabled={deleting}
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            <Icon name="x" />
          </button>
        </div>
        <div className="dialog-body">
          <p className="dialog-message">确定删除“{user.username}”吗？其文件会在后台继续清理。</p>
        </div>
        <div className="dialog-actions">
          <button
            type="button"
            className="button secondary"
            ref={returnButtonRef}
            disabled={deleting}
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            返回
          </button>
          <button
            type="button"
            className="button danger"
            disabled={deleting}
            onClick={(event) => onConfirm(event.currentTarget)}
          >
            {deleting ? "删除中..." : "删除用户"}
          </button>
        </div>
      </div>
    </DialogShell>
  );
}
