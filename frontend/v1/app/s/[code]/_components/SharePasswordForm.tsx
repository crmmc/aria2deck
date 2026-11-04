import type { ShareInfo } from "@/types";

type SharePasswordFormProps = {
  shareInfo: ShareInfo;
  password: string;
  passwordError: string;
  onPasswordChange: (value: string) => void;
  onSubmit: (e: React.FormEvent) => void;
};

export function SharePasswordForm({
  shareInfo,
  password,
  passwordError,
  onPasswordChange,
  onSubmit,
}: SharePasswordFormProps) {
  return (
    <div className="fixed inset-0 flex-center p-4">
      <div className="glass-frame animate-in max-w-400 w-full">
        <div className="text-center mb-7">
          <div className="text-4xl mb-4">🔒</div>
          <h2 className="text-lg mb-1" style={{ wordBreak: "break-all" }}>{shareInfo.file_name}</h2>
          <p className="muted">该分享需要提取码才能查看</p>
        </div>
        <form onSubmit={onSubmit}>
          <div className="mb-4">
            <input
              type="password"
              className="input"
              placeholder="请输入提取码"
              value={password}
              onChange={(e) => onPasswordChange(e.target.value)}
              required
              ref={(el) => el?.focus()}
              aria-label="提取码"
            />
          </div>
          {passwordError && (
            <div className="alert alert-danger text-center mb-4">{passwordError}</div>
          )}
          <button type="submit" className="button w-full">提取文件</button>
        </form>
      </div>
    </div>
  );
}
