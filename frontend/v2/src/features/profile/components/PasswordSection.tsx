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
    <section className="panel profile-panel">
      <div className="panel-head"><h2>修改密码</h2></div>
      <div className="panel-body">
        <form onSubmit={onSubmit} className="profile-form">
          {!isInitialPassword && (
            <div className="field">
              <label className="form-label field-label" htmlFor="profile-old-password">当前密码</label>
              <input id="profile-old-password" type="password" className="input" value={oldPassword} onChange={(event) => onOldPasswordChange(event.target.value)} required aria-label="当前密码" autoComplete="current-password" />
            </div>
          )}
          <div className="field">
            <label className="form-label field-label" htmlFor="profile-new-password">新密码</label>
            <input id="profile-new-password" type="password" className="input" value={newPassword} onChange={(event) => onNewPasswordChange(event.target.value)} minLength={6} required aria-label="新密码" autoComplete="new-password" />
            <p className="field-hint muted text-sm mt-1">至少 6 位字符</p>
          </div>
          <div className="field">
            <label className="form-label field-label" htmlFor="profile-confirm-password">确认新密码</label>
            <input id="profile-confirm-password" type="password" className="input" value={confirmPassword} onChange={(event) => onConfirmPasswordChange(event.target.value)} minLength={6} required aria-label="确认新密码" autoComplete="new-password" />
          </div>
          <div><button type="submit" className="button" disabled={changing}>{changing ? "修改中..." : "修改密码"}</button></div>
        </form>
      </div>
    </section>
  );
}
