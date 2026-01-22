"use client";

import { useAuth } from "@/lib/AuthContext";
import Sidebar from "@/components/Sidebar";

export default function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  const { user, loading, sidebarExpanded } = useAuth();

  if (loading) return null;

  return (
    <>
      <Sidebar user={user} />
      <div className="main-content" style={{ marginLeft: sidebarExpanded ? 292 : 100 }}>
        <div className="auth-container">
          {children}
        </div>
      </div>
    </>
  );
}
