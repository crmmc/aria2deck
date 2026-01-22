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

  const navItems = [
    { name: "任务", href: "/tasks", icon: "⬇️" },
    { name: "文件", href: "/files", icon: "📁" },
    { name: "历史", href: "/history", icon: "🕒" },
  ];

  if (user?.is_admin) {
    navItems.push({ name: "用户", href: "/users", icon: "👥" });
    navItems.push({ name: "设置", href: "/settings", icon: "⚙️" });
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
          <Link
            key={item.href}
            href={item.href}
            className={`nav-item ${isActive(item.href) ? "active" : ""}`}
          >
            <span className="nav-icon">{item.icon}</span>
            {sidebarExpanded && <span className="nav-text">{item.name}</span>}
          </Link>
        ))}
      </nav>

      <div className="sidebar-footer">
        <button
          onClick={logout}
          className="nav-item"
          style={{
            width: "100%",
            border: "none",
            background: "transparent",
            cursor: "pointer",
            textAlign: "left",
          }}
        >
          <span className="nav-icon">🚪</span>
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
