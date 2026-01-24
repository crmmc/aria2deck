"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense } from "react";
import { useAuth } from "@/lib/AuthContext";
import type { User } from "@/types";

type SidebarProps = {
  user: User | null;
};

function SidebarContent({ user }: SidebarProps) {
  const pathname = usePathname();
  const { logout, sidebarExpanded, setSidebarExpanded } = useAuth();

  // 基础导航项 - 所有用户可见
  type NavItem = {
    name: string;
    href: string;
    icon: React.ReactNode;
    dividerBefore?: boolean;
  };

  const navItems: NavItem[] = [
    {
      name: "任务",
      href: "/tasks",
      icon: (
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
      ),
    },
    {
      name: "文件",
      href: "/files",
      icon: (
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
      ),
    },
    {
      name: "历史",
      href: "/history",
      icon: (
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
      ),
    },
    // 用户设置 - 所有用户可见，齿轮图标
    {
      name: "设置",
      href: "/profile",
      icon: (
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
      ),
    },
  ];

  // 管理员专属导航项
  if (user?.is_admin) {
    navItems.push(
      // 多用户管理
      {
        name: "多用户管理",
        href: "/users",
        dividerBefore: true,
        icon: (
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
        ),
      },
      // 系统设置 - 服务器/控制台图标
      {
        name: "系统设置",
        href: "/settings",
        icon: (
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
        ),
      },
    );
  }

  const isActive = (href: string) => {
    if (!pathname) return false;
    // 精确匹配或子路径匹配
    return pathname === href || pathname.startsWith(href + "/");
  };

  return (
    <div
      className={`sidebar ${sidebarExpanded ? "expanded" : ""}`}
      onMouseEnter={() => !sidebarExpanded && setSidebarExpanded(true)}
      onMouseLeave={() => sidebarExpanded && setSidebarExpanded(false)}
    >
      <div className="sidebar-header">
        <div className="sidebar-logo">AC</div>
        {sidebarExpanded && (
          <span className="sidebar-title">Aria2 Controller</span>
        )}
      </div>

      <nav className="sidebar-nav">
        {navItems.map((item) => (
          <div key={item.href}>
            {item.dividerBefore && (
              <div className="nav-divider" />
            )}
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
        <button
          onClick={logout}
          className="nav-item logout-btn"
        >
          <span className="nav-icon">
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
          </span>
          {sidebarExpanded && <span className="nav-text">退出登录</span>}
        </button>
      </div>
    </div>
  );
}

export default function Sidebar(props: SidebarProps) {
  return (
    <Suspense fallback={<div className="sidebar"></div>}>
      <SidebarContent {...props} />
    </Suspense>
  );
}
