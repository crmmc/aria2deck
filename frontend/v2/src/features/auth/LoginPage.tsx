import { useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/app/hooks";
import { ApiRequestError } from "@/shared/api/client";
import { passwordClientHash } from "@/shared/lib/crypto";

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const usernameRef = useRef<HTMLInputElement>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  if (!loading && user !== null) {
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from && from !== "/login" ? from : "/tasks"} replace />;
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const trimmed = username.trim();
      const hashed = await passwordClientHash(trimmed, password);
      await login(trimmed, hashed);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/tasks", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiRequestError) {
        setError(caught.message);
      } else {
        setError("登录失败，请重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit} aria-label="登录表单">
        <h1 className="login-title">Aria2Deck</h1>
        <p className="login-sub">多用户下载管理平台</p>
        {error !== null && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}
        <label className="form-field">
          <span>用户名</span>
          <input
            ref={usernameRef}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            required
            maxLength={64}
          />
        </label>
        <label className="form-field">
          <span>密码</span>
          <span className="password-row">
            <input
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
            <button
              type="button"
              className="password-toggle"
              aria-label={showPassword ? "隐藏密码" : "显示密码"}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((current) => !current)}
            >
              {showPassword ? "隐藏" : "显示"}
            </button>
          </span>
        </label>
        <button type="submit" className="primary-button" disabled={submitting}>
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
