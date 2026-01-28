"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  // 重置密码模式
  const [resetMode, setResetMode] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  useEffect(() => {
    api
      .me()
      .then((user) => {
        if (user.is_initial_password) {
          // 需要重置密码
          setResetMode(true);
          setUsername(user.username);
        } else {
          router.push("/tasks");
        }
      })
      .catch(() => {});
  }, [router]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await api.login(username, password);
      window.location.href = "/tasks";
    } catch (err) {
      const message = (err as Error).message;
      if (message.includes("请先重置密码")) {
        setResetMode(true);
      } else {
        setError("用户名或密码无效");
      }
    }
  }

  async function onResetSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (newPassword.length < 6) {
      setError("密码长度至少为 6 位");
      return;
    }

    if (newPassword !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }

    try {
      await api.resetPassword(username, newPassword);
      window.location.href = "/tasks";
    } catch (err) {
      setError((err as Error).message || "重置密码失败");
    }
  }

  if (resetMode) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full">
          <div className="text-center mb-7">
            <h1 className="text-xl">重置密码</h1>
            <p className="muted">首次登录请设置新密码</p>
          </div>

          <form onSubmit={onResetSubmit}>
            <div className="mb-4">
              <input
                className="input"
                placeholder="用户名"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                autoFocus
              />
            </div>
            <div className="mb-4">
              <input
                className="input"
                type="password"
                placeholder="新密码（至少 6 位）"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                minLength={6}
                required
              />
            </div>
            <div className="mb-6">
              <input
                className="input"
                type="password"
                placeholder="确认新密码"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                minLength={6}
                required
              />
            </div>

            {error ? (
              <div className="alert alert-danger text-center mb-6">
                {error}
              </div>
            ) : null}

            <button className="button w-full text-md p-3" type="submit">
              设置密码并登录
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 flex-center p-4">
      <div className="glass-frame animate-in max-w-400 w-full">
        <div className="text-center mb-7">
          <h1 className="text-xl">登录</h1>
          <p className="muted">输入您的凭据以继续</p>
        </div>

        <form onSubmit={onSubmit}>
          <div className="mb-4">
            <input
              className="input"
              placeholder="用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
            />
          </div>
          <div className="mb-6">
            <input
              className="input"
              type="password"
              placeholder="密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
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
