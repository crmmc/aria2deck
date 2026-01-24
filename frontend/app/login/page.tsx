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
      window.location.href = "/tasks";
    } catch (err) {
      setError("用户名或密码无效");
    }
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
