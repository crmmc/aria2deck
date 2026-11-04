import type { FormEvent } from "react";

type CreateUserFormProps = {
  username: string;
  password: string;
  isAdmin: boolean;
  quotaValue: string;
  quotaUnit: string;
  error: string | null;
  onUsernameChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onAdminChange: (value: boolean) => void;
  onQuotaValueChange: (value: string) => void;
  onQuotaUnitChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
};

export function CreateUserForm({
  username,
  password,
  isAdmin,
  quotaValue,
  quotaUnit,
  error,
  onUsernameChange,
  onPasswordChange,
  onAdminChange,
  onQuotaValueChange,
  onQuotaUnitChange,
  onSubmit,
}: CreateUserFormProps) {
  return (
    <div className="card mb-7">
      <h3 className="mb-4">创建新用户</h3>
      <form onSubmit={onSubmit} className="create-user-form">
        <div className="create-user-fields">
          <div className="create-user-field">
            <label className="form-label" htmlFor="user-create-username">用户名</label>
            <input
              id="user-create-username"
              className="input"
              value={username}
              onChange={(e) => onUsernameChange(e.target.value)}
              required
              aria-label="用户名"
            />
          </div>
          <div className="create-user-field">
            <label className="form-label" htmlFor="user-create-password">密码</label>
            <input
              id="user-create-password"
              className="input"
              type="password"
              value={password}
              onChange={(e) => onPasswordChange(e.target.value)}
              required
              autoComplete="new-password"
              aria-label="密码"
            />
          </div>
          <div className="create-user-field">
            <label className="form-label" htmlFor="user-create-quota">存储配额</label>
            <div className="flex gap-2">
              <input
                id="user-create-quota"
                className="input flex-1"
                type="number"
                step="0.01"
                min="0.01"
                value={quotaValue}
                onChange={(e) => onQuotaValueChange(e.target.value)}
                required
                aria-label="存储配额"
              />
              <select
                className="input"
                value={quotaUnit}
                onChange={(e) => onQuotaUnitChange(e.target.value)}
                style={{ width: 80 }}
                aria-label="配额单位"
              >
                <option value="KB">KB</option>
                <option value="MB">MB</option>
                <option value="GB">GB</option>
              </select>
            </div>
          </div>
        </div>
        <div className="create-user-actions">
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={isAdmin}
              onChange={(e) => onAdminChange(e.target.checked)}
              aria-label="管理员"
            />
            <span className="text-base">管理员</span>
          </label>
          <button className="button" type="submit">
            创建用户
          </button>
        </div>
      </form>
      {error && (
        <p className="text-danger mt-3 text-base">{error}</p>
      )}
    </div>
  );
}
