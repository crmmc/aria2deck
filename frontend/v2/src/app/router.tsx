import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { AppLayout } from "@/app/layouts/AppLayout";
import { LoginPage } from "@/features/auth/LoginPage";
import { TasksPage } from "@/features/tasks/TasksPage";
import { HistoryPage } from "@/features/history/HistoryPage";
import { FilesPage } from "@/features/files/FilesPage";
import { SharesPage } from "@/features/shares/SharesPage";
import { ProfilePage } from "@/features/profile/ProfilePage";
import { UsersPage } from "@/features/users/UsersPage";
import { SettingsPage } from "@/features/settings/SettingsPage";
import { StoragePage } from "@/features/storage/StoragePage";
import { DiskOrphanPage } from "@/features/storage/DiskOrphanPage";
import { PublicSharePage } from "@/features/public-share/PublicSharePage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="page-loading" aria-busy="true">正在加载…</div>;
  if (user === null) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

function NotFoundPage() {
  return (
    <div className="empty-state">
      <h2>页面不存在</h2>
      <p>地址有误或页面已移除。</p>
    </div>
  );
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/* 公开分享页：独立于认证布局，未登录可直接访问与刷新。 */}
      <Route path="/s/:code" element={<PublicSharePage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/tasks" replace />} />
        <Route path="tasks" element={<TasksPage />} />
        <Route path="history" element={<HistoryPage />} />
        <Route path="files" element={<FilesPage />} />
        <Route path="shares" element={<SharesPage />} />
        <Route path="profile" element={<ProfilePage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="settings/:section?" element={<SettingsPage />} />
        <Route path="storage" element={<StoragePage />} />
        <Route path="storage/orphans" element={<DiskOrphanPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
