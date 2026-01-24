"use client";

import { useAuth } from "@/lib/AuthContext";
import Sidebar from "./Sidebar";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  const { user, loading, sidebarExpanded } = useAuth();

  if (loading) return null;

  return (
    <>
      <Sidebar user={user} />
      <div className={`main-content ${sidebarExpanded ? "sidebar-expanded" : ""}`}>
        <div className="auth-container">
          {children}
        </div>
      </div>
    </>
  );
}
