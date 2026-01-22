"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Check if already logged in
    api
      .me()
      .then(() => router.push("/tasks"))
      .catch(() => {});
  }, [router]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    try {
      await api.login(username, password);
      // Force a page reload to ensure AuthContext picks up the new session
      window.location.href = "/tasks";
    } catch (err) {
      setError("用户名或密码无效");
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
      }}
    >
      <div
        className="glass-frame animate-in"
        style={{ maxWidth: "400px", width: "100%" }}
      >
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <h1 style={{ fontSize: "24px" }}>登录</h1>
          <p className="muted">输入您的凭据以继续</p>
        </div>

        <form onSubmit={onSubmit}>
          <div style={{ marginBottom: 16 }}>
            <input
              className="input"
              placeholder="用户名"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
            />
          </div>
          <div style={{ marginBottom: 24 }}>
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
            <div
              style={{
                padding: "12px",
                borderRadius: "12px",
                background: "rgba(255, 59, 48, 0.1)",
                color: "var(--danger)",
                marginBottom: 24,
                fontSize: "14px",
                textAlign: "center",
              }}
            >
              {error}
            </div>
          ) : null}

          <button
            className="button"
            type="submit"
            style={{ width: "100%", fontSize: "16px", padding: "12px" }}
          >
            登录
          </button>
        </form>
      </div>
    </div>
  );
}
