"use client";

import { useState } from "react";
import { Outlet } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { Sidebar } from "@/app/components/Sidebar";
import { PasswordWarningBanner } from "@/app/components/PasswordWarningBanner";
import { BackendStatusBanner } from "@/app/components/BackendStatusBanner";
import { CommandPalette } from "@/app/components/CommandPalette";

export function AppLayout() {
  const { user, loading, error, retryAuth } = useAuth();
  const [sidebarExpanded, setSidebarExpanded] = useState(false);

  if (loading) {
    return (
      <div className="main-content">
        <div className="auth-container">
          <div className="glass-frame full-height animate-in">
            <div className="skeleton-header" />
            <div className="skeleton-content" />
          </div>
        </div>
      </div>
    );
  }

  // 显示服务器错误或网络错误（非 401）
  if (error && !user) {
    return (
      <div className="main-content">
        <div className="auth-container">
          <div className="glass-frame full-height animate-in">
            <div className="empty-state">
              <div className="empty-state-icon">
                <svg
                  width="48"
                  height="48"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <circle cx="12" cy="12" r="10" />
                  <line x1="12" y1="8" x2="12" y2="12" />
                  <line x1="12" y1="16" x2="12.01" y2="16" />
                </svg>
              </div>
              <p className="font-medium mb-1">连接失败</p>
              <p className="muted text-base mb-4">{error}</p>
              <button type="button"
                className="button"
                onClick={retryAuth}
              >
                重试
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar user={user} sidebarExpanded={sidebarExpanded} setSidebarExpanded={setSidebarExpanded} />
      <PasswordWarningBanner user={user} />
      <BackendStatusBanner user={user} />
      <div className="workspace">
        <main
          id="main"
          tabIndex={-1}
          className={`main-content${sidebarExpanded ? " sidebar-expanded" : ""}`}
        >
          <div className="auth-container">
            <Outlet />
          </div>
        </main>
      </div>
      <CommandPalette isAdmin={user?.is_admin ?? false} />
    </div>
  );
}
