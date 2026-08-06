import type { FormEvent } from "react";
import { ModalOverlay } from "@/components/ModalOverlay";
import type { User } from "@/types";
import { formatBytes } from "@/lib/utils";

type EditingUser = {
  id: number;
  username: string;
  password: string;
  is_admin: boolean;
  quota: number;
  quotaValue: string;
  quotaUnit: string;
  used_bytes?: number;
  reserved_bytes?: number;
  available_bytes?: number;
  usage_percent?: number;
  machine_share_percent?: number;
};

type EditUserDialogProps = {
  editingUser: EditingUser;
  currentUserId: number | undefined;
  editError: string | null;
  onFieldChange: (updates: Partial<EditingUser>) => void;
  onSubmit: (event: FormEvent) => void;
  onClose: () => void;
};

export type { EditingUser };

export function EditUserDialog({
  editingUser,
  currentUserId,
  editError,
  onFieldChange,
  onSubmit,
  onClose,
}: EditUserDialogProps) {
  return (
    <ModalOverlay
      onClose={onClose}
      ariaLabel="编辑用户"
      contentClassName="modal-content max-w-400 animate-in"
    >
        <h3 className="mb-5">编辑用户</h3>
        <form onSubmit={onSubmit}>
          <div className="form-group">
            <label className="form-label" htmlFor="user-edit-username">用户名</label>
            <input
              id="user-edit-username"
              className="input"
              value={editingUser.username}
              onChange={(e) => onFieldChange({ username: e.target.value })}
              required
              aria-label="用户名"
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="user-edit-password">
              新密码 <span className="muted font-normal">(留空保持不变)</span>
            </label>
            <input
              id="user-edit-password"
              className="input"
              type="password"
              value={editingUser.password}
              onChange={(e) => onFieldChange({ password: e.target.value })}
              placeholder="••••••••"
              autoComplete="new-password"
              aria-label="新密码"
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="user-edit-quota">存储配额</label>
            <div className="flex gap-2">
              <input
                id="user-edit-quota"
                className="input flex-1"
                type="number"
                step="0.01"
                min="0.01"
                value={editingUser.quotaValue}
                onChange={(e) => onFieldChange({ quotaValue: e.target.value })}
                required
                aria-label="存储配额"
              />
              <select
                className="input"
                value={editingUser.quotaUnit}
                onChange={(e) => onFieldChange({ quotaUnit: e.target.value })}
                style={{ width: 80 }}
                aria-label="配额单位"
              >
                <option value="KB">KB</option>
                <option value="MB">MB</option>
                <option value="GB">GB</option>
              </select>
            </div>
            {(editingUser.used_bytes !== undefined || editingUser.available_bytes !== undefined) && (
              <p className="muted text-xs mt-2">
                当前占用：{formatBytes(editingUser.used_bytes ?? 0)} 已用
                {" · "}
                {formatBytes(editingUser.reserved_bytes ?? 0)} 冻结
                {" · 可用 "}
                {formatBytes(editingUser.available_bytes ?? 0)}
                {editingUser.machine_share_percent !== undefined
                  ? ` · 全站 ${editingUser.machine_share_percent.toFixed(1)}%`
                  : ""}
              </p>
            )}
          </div>
          <div className="mb-5">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={editingUser.is_admin}
                onChange={(e) => onFieldChange({ is_admin: e.target.checked })}
                disabled={editingUser.id === currentUserId}
                aria-label="管理员用户"
              />
              <span className="text-base">管理员用户</span>
              {editingUser.id === currentUserId && (
                <span className="text-xs muted">(不能修改自己的角色)</span>
              )}
            </label>
          </div>
          {editError && (
            <p className="text-danger mb-4 text-base">{editError}</p>
          )}
          <div className="flex gap-3 flex-end">
            <button
              type="button"
              className="button secondary"
              onClick={onClose}
            >
              取消
            </button>
            <button type="submit" className="button">
              保存更改
            </button>
          </div>
        </form>
      </ModalOverlay>
  );
}
