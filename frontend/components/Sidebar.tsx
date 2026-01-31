"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useState, useEffect } from "react";
import { useAuth } from "@/lib/AuthContext";
import type { User } from "@/types";

type SidebarProps = {
  user: User | null;
};

// Icons as components for reuse
const TasksIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);

const HistoryIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="10" />
    <polyline points="12 6 12 12 16 14" />
  </svg>
);

const FilesIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
);

const ProfileIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

const UsersIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
  </svg>
);

const SettingsIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
    <line x1="8" y1="21" x2="16" y2="21" />
    <line x1="12" y1="17" x2="12" y2="21" />
    <path d="M6 8h.01" />
    <path d="M10 8h.01" />
    <path d="M14 8h8" />
  </svg>
);

const LogoutIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <polyline points="16 17 21 12 16 7" />
    <line x1="21" y1="12" x2="9" y2="12" />
  </svg>
);

const MoreIcon = () => (
  <svg
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <circle cx="12" cy="12" r="1" />
    <circle cx="12" cy="5" r="1" />
    <circle cx="12" cy="19" r="1" />
  </svg>
);

function SidebarContent({ user }: SidebarProps) {
  const pathname = usePathname();
  const { logout, sidebarExpanded, setSidebarExpanded } = useAuth();
  const [isMobile, setIsMobile] = useState(false);
  const [showMoreMenu, setShowMoreMenu] = useState(false);

  // Detect mobile viewport
  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768);
    };
    checkMobile();
    window.addEventListener("resize", checkMobile);
    return () => window.removeEventListener("resize", checkMobile);
  }, []);

  // Close more menu when navigating
  useEffect(() => {
    setShowMoreMenu(false);
  }, [pathname]);

  const isActive = (href: string) => {
    if (!pathname) return false;
    return pathname === href || pathname.startsWith(href + "/");
  };

  // Mobile bottom navigation
  const bottomNavItems = [
    { name: "任务", href: "/tasks", icon: <TasksIcon /> },
    { name: "历史", href: "/history", icon: <HistoryIcon /> },
    { name: "文件", href: "/files", icon: <FilesIcon /> },
    { name: "设置", href: "/profile", icon: <ProfileIcon /> },
  ];

  return (
    <>
      {/* Mobile Bottom Nav - Hidden on Desktop via CSS */}
      <nav className="bottom-nav">
        {bottomNavItems.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`bottom-nav-item ${isActive(item.href) ? "active" : ""}`}
          >
            <span className="bottom-nav-icon">{item.icon}</span>
            <span className="bottom-nav-label">{item.name}</span>
          </Link>
        ))}
        <button
          type="button"
          className={`bottom-nav-item ${showMoreMenu ? "active" : ""}`}
          onClick={() => setShowMoreMenu(true)}
        >
          <span className="bottom-nav-icon">
            <MoreIcon />
          </span>
          <span className="bottom-nav-label">更多</span>
        </button>
      </nav>

      {/* More menu overlay */}
      {showMoreMenu && (
        <div className="more-menu-overlay" onClick={() => setShowMoreMenu(false)}>
          <div className="more-menu" onClick={(e) => e.stopPropagation()}>
            <div className="more-menu-header">
              <h3 className="more-menu-title">更多</h3>
              <button
                type="button"
                className="more-menu-close"
                onClick={() => setShowMoreMenu(false)}
              >
                ×
              </button>
            </div>

            {user?.is_admin && (
              <>
                <Link
                  href="/users"
                  className="more-menu-item"
                  onClick={() => setShowMoreMenu(false)}
                >
                  <span className="more-menu-icon">
                    <UsersIcon />
                  </span>
                  多用户管理
                </Link>
                <Link
                  href="/settings"
                  className="more-menu-item"
                  onClick={() => setShowMoreMenu(false)}
                >
                  <span className="more-menu-icon">
                    <SettingsIcon />
                  </span>
                  系统设置
                </Link>
                <div className="more-menu-divider" />
              </>
            )}

            <button
              type="button"
              className="more-menu-item danger"
              onClick={() => {
                setShowMoreMenu(false);
                logout();
              }}
            >
              <span className="more-menu-icon">
                <LogoutIcon />
              </span>
              退出登录
            </button>
          </div>
        </div>
      )}

      {/* Desktop sidebar - Hidden on Mobile via CSS */}
      <div
        className={`sidebar ${sidebarExpanded ? "expanded" : ""}`}
        onMouseEnter={() => !sidebarExpanded && setSidebarExpanded(true)}
        onMouseLeave={() => sidebarExpanded && setSidebarExpanded(false)}
      >
        <div className="sidebar-header">
          <div className="sidebar-logo">AD</div>
          {sidebarExpanded && <span className="sidebar-title">Aria2Deck</span>}
        </div>

        <nav className="sidebar-nav">
          {[
            { name: "任务", href: "/tasks", icon: <TasksIcon /> },
            { name: "任务历史", href: "/history", icon: <HistoryIcon /> },
            { name: "文件", href: "/files", icon: <FilesIcon /> },
            { name: "设置", href: "/profile", icon: <ProfileIcon /> },
            ...(user?.is_admin
              ? [
                  {
                    name: "多用户管理",
                    href: "/users",
                    dividerBefore: true,
                    icon: <UsersIcon />,
                  },
                  { name: "系统设置", href: "/settings", icon: <SettingsIcon /> },
                ]
              : []),
          ].map((item) => (
            <div key={item.href}>
              {item.dividerBefore && <div className="nav-divider" />}
              <Link
                href={item.href}
                className={`nav-item ${isActive(item.href) ? "active" : ""}`}
              >
                <span className="nav-icon">{item.icon}</span>
                {sidebarExpanded && <span className="nav-text">{item.name}</span>}
              </Link>
            </div>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button onClick={logout} className="nav-item logout-btn">
            <span className="nav-icon">
              <LogoutIcon />
            </span>
            {sidebarExpanded && <span className="nav-text">退出登录</span>}
          </button>
        </div>
      </div>
    </>
  );
}

export default function Sidebar(props: SidebarProps) {
  return (
    <Suspense fallback={<div className="sidebar"></div>}>
      <SidebarContent {...props} />
    </Suspense>
  );
}
