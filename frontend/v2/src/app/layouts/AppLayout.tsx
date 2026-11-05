import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth, useToast } from "@/app/hooks";
import { api } from "@/shared/api/client";
import type { BackendStatusResponse } from "@/shared/api/types";
import { formatSpeed } from "@/shared/lib/format";
import type { TaskStatsResponse } from "@/shared/api/types";

const BASE_NAV_ITEMS = [
  { to: "/tasks", label: "任务" },
  { to: "/history", label: "历史" },
  { to: "/files", label: "文件" },
  { to: "/shares", label: "分享" },
  { to: "/profile", label: "我的" },
] as const;

const ADMIN_NAV_ITEMS = [
  { to: "/users", label: "用户" },
  { to: "/settings", label: "设置" },
  { to: "/storage", label: "存储" },
] as const;

interface SystemStatusEnvelope {
  download_backend: BackendStatusResponse;
}

export function AppLayout() {
  const { user, logout } = useAuth();
  const { push } = useToast();
  const navigate = useNavigate();
  const [status, setStatus] = useState<BackendStatusResponse | null>(null);
  const [stats, setStats] = useState<TaskStatsResponse | null>(null);

  const navItems = useMemo(
    () => (user?.is_admin ? [...BASE_NAV_ITEMS, ...ADMIN_NAV_ITEMS] : [...BASE_NAV_ITEMS]),
    [user],
  );

  const handleLogout = useCallback(async () => {
    await logout();
    navigate("/login", { replace: true });
  }, [logout, navigate]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const value = await api.get<SystemStatusEnvelope>("/api/v2/system/status");
        if (!cancelled) setStatus(value.download_backend);
      } catch {
        /* 轮询失败保持上次状态 */
      }
    };
    void poll();
    const timer = window.setInterval(poll, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (user === null) return;
    let cancelled = false;
    void api
      .get<TaskStatsResponse>("/api/v2/tasks/stats")
      .then((value) => {
        if (!cancelled) setStats(value);
      })
      .catch(() => {
        /* 统计失败不阻塞布局 */
      });
    return () => {
      cancelled = true;
    };
  }, [user]);

  return (
    <div className="app-shell">
      <aside className="app-sidebar" aria-label="主导航">
        <div className="app-brand">
          <span className="app-brand-title">Aria2Deck</span>
          <span className="app-brand-sub">{user?.username ?? ""}</span>
        </div>
        <nav className="app-nav">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `app-nav-link${isActive ? " active" : ""}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        {stats !== null && (
          <div className="app-stats" aria-label="任务统计">
            <p>下载速度：{formatSpeed(stats.download_speed)}</p>
            <p>任务总数：{stats.total}</p>
          </div>
        )}
        <button type="button" className="app-logout" onClick={() => void handleLogout()}>
          登出
        </button>
      </aside>
      {status?.status === "degraded" && (
        <div className="backend-banner" role="alert">
          {status.message}
        </div>
      )}
      <main className="app-main">
        <Outlet />
      </main>
      <nav className="app-bottomnav" aria-label="底部导航">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `app-bottomnav-link${isActive ? " active" : ""}`}
          >
            {item.label}
          </NavLink>
        ))}
        <button
          type="button"
          className="app-bottomnav-link"
          onClick={() => {
            push("info", "已触发登出");
            void handleLogout();
          }}
        >
          登出
        </button>
      </nav>
    </div>
  );
}
