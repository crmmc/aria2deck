import { useEffect, useRef, type FormEvent } from "react";

import { DialogShell } from "@/shared/components/DialogShell";
import { Icon } from "@/shared/components/Icon";
import { formatAdminBytes } from "@/shared/lib/format";
import { requestModalClose } from "@/shared/ui/modalClose";

export type EditingUser = {
  id: number;
  originalUsername: string;
  username: string;
  password: string;
  is_admin: boolean;
  originalQuotaValue: string;
  quotaValue: string;
  used_bytes: number;
  reserved_bytes: number;
  available_bytes: number;
};

type EditUserDialogProps = {
  editingUser: EditingUser;
  currentUserId: number | undefined;
  editError: string | null;
  onFieldChange: (updates: Partial<EditingUser>) => void;
  onSubmit: (event: FormEvent) => void;
  onClose: () => void;
};

export function EditUserDialog({
  editingUser,
  currentUserId,
  editError,
  onFieldChange,
  onSubmit,
  onClose,
}: EditUserDialogProps) {
  const current = editingUser.id === currentUserId;
  const usernameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    usernameInputRef.current?.focus();
  }, []);

  return (
    <DialogShell
      onClose={onClose}
      ariaLabel={`编辑用户 ${editingUser.originalUsername}`}
      contentClassName="modal-content admin-dialog-content"
    >
      <div className="dialog-shell">
        <div className="dialog-head">
          <h2>编辑用户 {editingUser.originalUsername}</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="关闭对话框"
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            <Icon name="x" />
          </button>
        </div>
        <div className="dialog-body">
          <form id="admin-user-edit-form" onSubmit={onSubmit} noValidate>
            <p className="admin-edit-usage">
              当前已使用 {formatAdminBytes(editingUser.used_bytes)}，预留 {formatAdminBytes(editingUser.reserved_bytes)}，可用 {formatAdminBytes(editingUser.available_bytes)}。
            </p>
            <div className="form-grid">
              <div className="field">
                <label className="form-label field-label" htmlFor="edit-user-name">用户名</label>
                <input
                  id="edit-user-name"
                  ref={usernameInputRef}
                  className="input"
                  value={editingUser.username}
                  onChange={(e) => onFieldChange({ username: e.target.value })}
                  required
                />
                <p className="field-note" />
              </div>
              <div className="field">
                <label className="form-label field-label" htmlFor="edit-user-password">新密码</label>
                <input
                  id="edit-user-password"
                  className="input"
                  type="password"
                  value={editingUser.password}
                  onChange={(e) => onFieldChange({ password: e.target.value })}
                  autoComplete="new-password"
                />
                <p className="field-note">留空表示不修改；修改用户名时必须填写。</p>
              </div>
              <div className="field">
                <label className="form-label field-label" htmlFor="edit-user-quota">存储配额</label>
                <div className="number-unit">
                  <input
                    id="edit-user-quota"
                    className="input"
                    type="number"
                    inputMode="decimal"
                    step="any"
                    min="0"
                    value={editingUser.quotaValue}
                    onChange={(e) => onFieldChange({ quotaValue: e.target.value })}
                    required
                  />
                  <input
                    id="edit-user-quota-unit"
                    className="input"
                    value="GB"
                    aria-label="存储配额单位"
                    readOnly
                  />
                </div>
                <p className="field-note" />
              </div>
              <div className="field">
                <label className="form-label field-label" htmlFor="edit-user-role">角色</label>
                <div className="select-wrap">
                  <select
                    id="edit-user-role"
                    className="select"
                    value={editingUser.is_admin ? "admin" : "user"}
                    onChange={(e) => onFieldChange({ is_admin: e.target.value === "admin" })}
                    disabled={current}
                  >
                    <option value="user">普通用户</option>
                    <option value="admin">管理员</option>
                  </select>
                  <Icon name="chevron-down" />
                </div>
                <p className="field-note">{current ? "当前账号的角色不可修改。" : ""}</p>
              </div>
            </div>
            <p className="field-error" id="admin-user-edit-error" role="alert">{editError ?? ""}</p>
          </form>
        </div>
        <div className="dialog-actions">
          <button
            type="button"
            className="button secondary"
            onClick={(event) => requestModalClose(event.currentTarget)}
          >
            取消
          </button>
          <button type="submit" form="admin-user-edit-form" className="button">
            保存修改
          </button>
        </div>
      </div>
    </DialogShell>
  );
}
