"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { ApiRequestError } from "@/shared/api/client";
import { passwordClientHash } from "@/shared/lib/crypto";
import { useSiteTitle } from "@/shared/lib/siteTitle";

function EyeIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
      <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
      <path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
      <line x1="2" y1="2" x2="22" y2="22" />
    </svg>
  );
}

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const usernameInputRef = useRef<HTMLInputElement>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const siteTitle = useSiteTitle();

  useEffect(() => {
    usernameInputRef.current?.focus();
  }, []);

  useEffect(() => {
    document.title = siteTitle;
  }, [siteTitle]);

  if (!loading && user !== null) {
    // 初始密码用户引导到个人资料页改密（C25 保留查询参数提示）。
    if (user.is_initial_password) {
      return <Navigate to="/profile?initial_password=1" replace />;
    }
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from && from !== "/login" ? from : "/tasks"} replace />;
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      const trimmed = username.trim();
      const hashed = await passwordClientHash(trimmed, password);
      const me = await login(trimmed, hashed);
      if (me.is_initial_password) {
        navigate("/profile?initial_password=1", { replace: true });
        return;
      }
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/tasks", { replace: true });
    } catch (caught) {
      if (caught instanceof ApiRequestError) {
        if (caught.status === 401) {
          setError("用户名或密码无效");
          return;
        }
        if (caught.status === 0) {
          setError("网络连接失败，请检查地址或网络");
          return;
        }
        setError(caught.message || "登录失败，请稍后重试");
        return;
      }
      if (caught instanceof Error) {
        setError(caught.message || "登录失败，请稍后重试");
        return;
      }
      setError("登录失败，请稍后重试");
    }
  }

  return (
    <div className="fixed inset-0 flex-center p-4">
      <div className="glass-frame animate-in max-w-400 w-full">
        <div className="text-center mb-7">
          <h1 className="text-xl">{siteTitle}</h1>
          <p className="muted">输入您的凭据以继续</p>
        </div>

        <form onSubmit={onSubmit}>
          <div className="mb-4">
            <input
              className="input"
              aria-label="用户名"
              placeholder="用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              ref={usernameInputRef}
            />
          </div>
          <div className="mb-6">
            <div className="password-input-wrap">
              <input
                className="input password-input"
                aria-label="密码"
                type={passwordVisible ? "text" : "password"}
                placeholder="密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <button
                aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
                className="password-visibility-button"
                onClick={() => setPasswordVisible((visible) => !visible)}
                type="button"
              >
                {passwordVisible ? <EyeOffIcon /> : <EyeIcon />}
              </button>
            </div>
          </div>

          {error ? (
            <div className="alert alert-danger text-center mb-6">
              {error}
            </div>
          ) : null}

          <button className="button w-full text-md p-3" type="submit">
            登录
          </button>
        </form>
      </div>
    </div>
  );
}
