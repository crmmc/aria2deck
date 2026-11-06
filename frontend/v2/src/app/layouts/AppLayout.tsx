import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth, useToast } from "@/app/hooks";
import { api, getNetworkOffline, subscribeNetworkHealth } from "@/shared/api/client";
import type { BackendStatusResponse, TaskStatsResponse } from "@/shared/api/types";
import { formatBytes, formatSpeed } from "@/shared/lib/format";
import { onTaskActivity } from "@/shared/lib/taskEvents";
import { useSiteTitle } from "@/shared/lib/siteTitle";

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

const STATS_POLL_MS = 5000;
const STATS_DEBOUNCE_MS = 1000;

export function AppLayout() {
  const { user, logout, refresh } = useAuth();
  const { push } = useToast();
  const navigate = useNavigate();
  const siteTitle = useSiteTitle();
  const networkOffline = useSyncExternalStore(subscribeNetworkHealth, getNetworkOffline);
  const [status, setStatus] = useState<BackendStatusResponse | null>(null);
  const [stats, setStats] = useState<TaskStatsResponse | null>(null);
  const statsSeq = useRef(0);

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

  // 统计实时（G11）：5s 轮询 + 任务活动事件（WS 变更方发布）防抖立即刷新。
  const refreshStats = useCallback(async () => {
    const seq = ++statsSeq.current;
    try {
      const value = await api.get<TaskStatsResponse>("/api/v2/tasks/stats");
      if (seq === statsSeq.current) setStats(value);
    } catch {
      /* 统计失败保持上次读数 */
    }
  }, []);

  useEffect(() => {
    if (user === null) return;
    void refreshStats();
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshStats();
    }, STATS_POLL_MS);
    let debounce: number | undefined;
    const offActivity = onTaskActivity(() => {
      window.clearTimeout(debounce);
      debounce = window.setTimeout(() => void refreshStats(), STATS_DEBOUNCE_MS);
    });
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(debounce);
      offActivity();
    };
  }, [user, refreshStats]);

  return (
    <div className="app-shell">
      <aside className="app-sidebar" aria-label="主导航">
        <div className="app-brand">
          <span className="app-brand-title">{siteTitle}</span>
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
        {stats !== null && <StatsPanel stats={stats} />}
        <button type="button" className="app-logout" onClick={() => void handleLogout()}>
          登出
        </button>
      </aside>
      {(user?.is_initial_password === true || networkOffline || status?.status === "degraded") && (
        <div className="app-banners">
          {user?.is_initial_password === true && (
            <div className="initial-password-banner" role="alert">
              <span>您正在使用初始密码，请尽快修改以确保账户安全</span>
              <button type="button" onClick={() => navigate("/profile")}>
                前往修改
              </button>
            </div>
          )}
          {networkOffline && (
            <div className="connection-banner" role="alert">
              <span>连接失败，请检查网络后重试</span>
              <button type="button" onClick={() => void refresh()}>
                重试
              </button>
            </div>
          )}
          {status?.status === "degraded" && (
            <div className="backend-banner" role="alert">
              {status.message}
            </div>
          )}
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

/** 侧栏统计（G11，旧版 StatsWidget 口径）：上下行速度、活跃任务、可用空间进度与冻结。 */
function StatsPanel({ stats }: { stats: TaskStatsResponse }) {
  const quota = stats.quota;
  const usedPercent =
    quota.quota_bytes > 0
      ? Math.min(100, Math.round((quota.used_bytes / quota.quota_bytes) * 100))
      : 0;
  return (
    <div className="app-stats" aria-label="任务统计">
      <p>
        ↓ {formatSpeed(stats.download_speed)} · ↑ {formatSpeed(stats.upload_speed)}
      </p>
      <p>活跃任务：{stats.counts.active}</p>
      <p>
        可用空间：{formatBytes(quota.available_bytes)} / {formatBytes(quota.quota_bytes)}
      </p>
      <div
        className="progress-track stats-progress"
        role="progressbar"
        aria-label="空间用量"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={usedPercent}
      >
        <div className="progress-fill" style={{ width: `${usedPercent}%` }} />
      </div>
      {quota.reserved_bytes > 0 && (
        <p className="stats-frozen">已冻结 {formatBytes(quota.reserved_bytes)}（下载中）</p>
      )}
    </div>
  );
}
