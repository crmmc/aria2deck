"use client";

import { useAuth } from "@/lib/AuthContext";
import Sidebar from "@/components/Sidebar";
import PasswordWarningBanner from "@/components/PasswordWarningBanner";

export default function AuthenticatedLayout({ children }: { children: React.ReactNode }) {
  const { user, loading, sidebarExpanded } = useAuth();

  if (loading) return null;

  return (
    <>
      <Sidebar user={user} />
      <PasswordWarningBanner user={user} />
      <div className="main-content" style={{ marginLeft: sidebarExpanded ? 292 : 100 }}>
        <div className="auth-container">
          {children}
        </div>
      </div>
    </>
  );
}
