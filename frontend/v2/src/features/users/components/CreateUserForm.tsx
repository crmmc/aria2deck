import type { FormEvent } from "react";

import { Icon } from "@/shared/components/Icon";

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
    <section className="panel quick-add admin-create-panel" aria-label="创建用户">
      <div className="settings-section-head">
        <h2>创建新用户</h2>
        <p>设置账号、角色与初始存储配额。</p>
      </div>
      <form id="admin-user-create-form" onSubmit={onSubmit} noValidate>
        <div className="admin-user-create-grid">
          <div className="field">
            <label className="form-label field-label" htmlFor="create-user-name">用户名</label>
            <input
              id="create-user-name"
              className="input"
              value={username}
              onChange={(e) => onUsernameChange(e.target.value)}
              required
              autoComplete="off"
            />
          </div>
          <div className="field">
            <label className="form-label field-label" htmlFor="create-user-password">密码</label>
            <input
              id="create-user-password"
              className="input"
              type="password"
              value={password}
              onChange={(e) => onPasswordChange(e.target.value)}
              required
              autoComplete="new-password"
            />
          </div>
          <div className="field">
            <label className="form-label field-label" htmlFor="create-user-quota">存储配额</label>
            <div className="number-unit">
              <input
                id="create-user-quota"
                className="input"
                type="number"
                inputMode="decimal"
                step="any"
                min="0"
                value={quotaValue}
                onChange={(e) => onQuotaValueChange(e.target.value)}
                required
              />
              <div className="select-wrap">
                <select
                  id="create-user-quota-unit"
                  className="select"
                  value={quotaUnit}
                  onChange={(e) => onQuotaUnitChange(e.target.value)}
                  aria-label="存储配额单位"
                >
                  <option value="KB">KB</option>
                  <option value="MB">MB</option>
                  <option value="GB">GB</option>
                </select>
                <Icon name="chevron-down" />
              </div>
            </div>
          </div>
          <label className="admin-checkbox" htmlFor="create-user-admin">
            <input
              id="create-user-admin"
              type="checkbox"
              checked={isAdmin}
              onChange={(e) => onAdminChange(e.target.checked)}
            />
            管理员
          </label>
          <button className="button admin-create-submit" type="submit">
            <Icon name="plus" />创建用户
          </button>
          {error && (
            <p className="field-error admin-create-error" id="admin-user-create-error" role="alert">
              {error}
            </p>
          )}
        </div>
      </form>
    </section>
  );
}
