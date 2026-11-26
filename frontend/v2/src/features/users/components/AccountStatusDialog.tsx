import { useEffect, useRef } from "react";

import type { UserResponse } from "@/shared/api/types";
import { DialogShell } from "@/shared/components/DialogShell";
import { Icon } from "@/shared/components/Icon";
import { requestModalClose } from "@/shared/ui/modalClose";

type AccountStatusDialogProps = {
  user: UserResponse;
  next: "active" | "frozen";
  busy: boolean;
  error: string | null;
  onConfirm: (trigger: Element) => void;
  onClose: () => void;
};

/** 冻结/解冻确认：请求期间禁止关闭与重复提交，失败留在弹窗内可重试。 */
export function AccountStatusDialog({
  user,
  next,
  busy,
  error,
  onConfirm,
  onClose,
}: AccountStatusDialogProps) {
  const returnButtonRef = useRef<HTMLButtonElement>(null);
  const freezing = next === "frozen";

  useEffect(() => {
    returnButtonRef.current?.focus();
  }, []);

  return (
    <DialogShell
      onClose={onClose}
      ariaLabel={`${freezing ? "冻结" : "解冻"}用户 ${user.username}`}
      contentClassName="modal-content admin-dialog-content"
      closeDisabled={busy}
    >
      <div className="dialog-shell">
        <div className="dialog-head">
          <h2>{freezing ? "冻结用户" : "解冻用户"}</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="关闭对话框"
            disabled={busy}
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            <Icon name="x" />
          </button>
        </div>
        <div className="dialog-body">
          <p className="dialog-message">
            {freezing
              ? `确定冻结“${user.username}”吗？该账号会立即退出登录，之后无法登录或使用 API Token 与 RPC 访问；文件、分享和正在运行的任务不会被删除。`
              : `确定解冻“${user.username}”吗？解冻后可以重新登录，但之前被撤销的会话不会恢复。`}
          </p>
          <p className="field-error" role="alert">{error ?? ""}</p>
        </div>
        <div className="dialog-actions">
          <button
            type="button"
            className="button secondary"
            ref={returnButtonRef}
            disabled={busy}
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            取消
          </button>
          <button
            type="button"
            className={freezing ? "button danger" : "button"}
            disabled={busy}
            onClick={(event) => onConfirm(event.currentTarget)}
          >
            {busy ? "处理中..." : freezing ? "冻结账号" : "解冻账号"}
          </button>
        </div>
      </div>
    </DialogShell>
  );
}
