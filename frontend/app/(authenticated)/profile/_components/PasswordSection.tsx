import type { FormEvent } from "react";

type PasswordSectionProps = {
  isInitialPassword: boolean;
  oldPassword: string;
  newPassword: string;
  confirmPassword: string;
  changing: boolean;
  onOldPasswordChange: (value: string) => void;
  onNewPasswordChange: (value: string) => void;
  onConfirmPasswordChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
};

export function PasswordSection({
  isInitialPassword,
  oldPassword,
  newPassword,
  confirmPassword,
  changing,
  onOldPasswordChange,
  onNewPasswordChange,
  onConfirmPasswordChange,
  onSubmit,
}: PasswordSectionProps) {
  return (
    <div className="card mb-6">
      <h2 className="section-title">修改密码</h2>
      <form onSubmit={onSubmit} className="max-w-400">
        {!isInitialPassword && (
          <div className="mb-4">
            <label className="form-label" htmlFor="profile-old-password">当前密码</label>
            <input
              id="profile-old-password"
              type="password"
              className="input"
              value={oldPassword}
              onChange={(e) => onOldPasswordChange(e.target.value)}
              required
              aria-label="当前密码"
            />
          </div>
        )}
        <div className="mb-4">
          <label className="form-label" htmlFor="profile-new-password">新密码</label>
          <input
            id="profile-new-password"
            type="password"
            className="input"
            value={newPassword}
            onChange={(e) => onNewPasswordChange(e.target.value)}
            minLength={6}
            required
            aria-label="新密码"
          />
          <p className="muted text-sm mt-1">至少 6 位字符</p>
        </div>
        <div className="mb-6">
          <label className="form-label" htmlFor="profile-confirm-password">确认新密码</label>
          <input
            id="profile-confirm-password"
            type="password"
            className="input"
            value={confirmPassword}
            onChange={(e) => onConfirmPasswordChange(e.target.value)}
            minLength={6}
            required
            aria-label="确认新密码"
          />
        </div>
        <button
          type="submit"
          className="button"
          disabled={changing}
        >
          {changing ? "修改中..." : "修改密码"}
        </button>
      </form>
    </div>
  );
}
