import { useEffect, useRef } from "react";

import type { UserResponse } from "@/shared/api/types";
import { DialogShell } from "@/shared/components/DialogShell";
import { Icon } from "@/shared/components/Icon";
import { useClipboard } from "@/shared/hooks/useClipboard";
import { requestModalClose } from "@/shared/ui/modalClose";

type ResetPasswordDialogProps = {
  user: UserResponse;
  busy: boolean;
  error: string | null;
  temporaryPassword: string | null;
  onConfirm: () => void;
  onClose: () => void;
};

/** 随机重置：确认 → 忙碌 → 一次性展示明文；失败留在弹窗内可重试，关闭即丢弃明文。 */
export function ResetPasswordDialog({
  user,
  busy,
  error,
  temporaryPassword,
  onConfirm,
  onClose,
}: ResetPasswordDialogProps) {
  const returnButtonRef = useRef<HTMLButtonElement>(null);
  const copyButtonRef = useRef<HTMLButtonElement>(null);
  const copyToClipboard = useClipboard();
  const done = temporaryPassword !== null;

  useEffect(() => {
    if (done) copyButtonRef.current?.focus();
    else returnButtonRef.current?.focus();
  }, [done]);

  return (
    <DialogShell
      onClose={onClose}
      ariaLabel={`重置用户 ${user.username} ${done ? "的密码" : "密码"}`}
      contentClassName="modal-content admin-dialog-content"
      closeDisabled={busy}
    >
      <div className="dialog-shell">
        <div className="dialog-head">
          <h2>{done ? "密码已重置" : "重置密码"}</h2>
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
          {done ? (
            <>
              <p className="dialog-message">
                “{user.username}” 的新密码已生成，关闭后将无法再次查看，只能重新重置。
              </p>
              <div className="field">
                <label className="form-label field-label" htmlFor="reset-temporary-password">
                  临时密码
                </label>
                <div className="number-unit">
                  <input
                    id="reset-temporary-password"
                    className="input"
                    value={temporaryPassword}
                    readOnly
                    onFocus={(event) => event.currentTarget.select()}
                  />
                  <button
                    ref={copyButtonRef}
                    className="icon-btn"
                    type="button"
                    aria-label="复制临时密码"
                    title="复制"
                    onClick={() =>
                      void copyToClipboard(temporaryPassword, {
                        successMessage: "临时密码已复制",
                      })
                    }
                  >
                    <Icon name="copy" className="icon-sm" />
                  </button>
                </div>
                <p className="field-note">
                  交给用户后，请让对方登录并在个人资料页自行修改密码。
                </p>
              </div>
            </>
          ) : (
            <p className="dialog-message">
              确定重置“{user.username}”的密码吗？系统会生成一个随机密码并只显示一次，旧密码和已登录会话立即失效。
            </p>
          )}
          <p className="field-error" role="alert">{error ?? ""}</p>
        </div>
        <div className="dialog-actions">
          {done ? (
            <button
              type="button"
              className="button"
              onClick={(event) => requestModalClose(event.currentTarget)}
            >
              关闭
            </button>
          ) : (
            <>
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
                className="button danger"
                disabled={busy}
                onClick={() => onConfirm()}
              >
                {busy ? "重置中..." : error ? "重试" : "重置密码"}
              </button>
            </>
          )}
        </div>
      </div>
    </DialogShell>
  );
}
