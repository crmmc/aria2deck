"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { useToast } from "@/shared/components/toast-context";
import { PageHeader } from "@/shared/components/PageHeader";
import { errorMessage } from "@/shared/api/client";
import { requestModalClose } from "@/shared/ui/modalClose";
import type { UserResponse } from "@/shared/api/types";

import {
  createUser,
  deleteUser,
  fetchUsersAll,
  formatQuotaForForm,
  parseQuotaBytes,
  updateUser,
} from "./api";
import { toUserViews, type UserView } from "./model";
import { CreateUserForm } from "./components/CreateUserForm";
import { UsersTable } from "./components/UsersTable";
import { EditUserDialog, type EditingUser } from "./components/EditUserDialog";
import { DeleteUserDialog } from "./components/DeleteUserDialog";

const DEFAULT_QUOTA_VALUE = "10";
const DEFAULT_QUOTA_UNIT = "GB";

/** 归档参考 用户管理页原文迁移（react-router 版）：状态域、表单流程和弹窗与 归档参考 一致。 */
export function UsersPage() {
  const navigate = useNavigate();
  const { user: currentUser, refresh } = useAuth();
  const { showToast } = useToast();
  const [users, setUsers] = useState<UserResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [quotaValue, setQuotaValue] = useState(DEFAULT_QUOTA_VALUE);
  const [quotaUnit, setQuotaUnit] = useState(DEFAULT_QUOTA_UNIT);
  const [error, setError] = useState<string | null>(null);

  const [editingUser, setEditingUser] = useState<EditingUser | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  const [deletingUser, setDeletingUser] = useState<UserView | null>(null);
  const [deleting, setDeleting] = useState(false);

  // 全站占比的分母是全部用户 used 之和，任何增删改后都按全量重算（C17，归档参考 list_users 同口径）。
  const userViews = useMemo(() => toUserViews(users), [users]);

  const authReady = currentUser !== null;
  const isAdminUser = currentUser?.is_admin === true;

  const loadUsers = useCallback(async () => {
    try {
      // C21：归档参考 无分页界面，数据层顺序聚合全部 V2 页。
      setUsers(await fetchUsersAll());
    } catch (caught) {
      setLoadError(errorMessage(caught, "加载用户列表失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdminUser) {
      void loadUsers();
      return;
    }
    if (!authReady) return;
    // 归档参考 page.tsx:47-52：非管理员回任务页，且绝不请求用户列表。
    setLoading(false);
    navigate("/tasks", { replace: true });
  }, [authReady, isAdminUser, loadUsers, navigate]);

  async function handleCreateUser(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (username.trim() === "" || password === "") {
      setError("请填写用户名和密码");
      return;
    }
    const quotaBytes = parseQuotaBytes(quotaValue, quotaUnit);
    if (quotaBytes === null) {
      setError("配额必须为正数");
      return;
    }

    try {
      // 归档参考 后端创建用户一律标记初始密码，V2 默认 false，必须显式传 true（见 api.createUser）。
      const created = await createUser({ username, password, isAdmin, quotaBytes });
      setUsers((prev) => [...prev, created]);
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      setQuotaValue(DEFAULT_QUOTA_VALUE);
      setQuotaUnit(DEFAULT_QUOTA_UNIT);
      showToast("用户已创建", "success");
    } catch (caught) {
      setError(errorMessage(caught, "创建用户失败"));
    }
  }

  function openEditModal(user: UserView) {
    const { quotaValue } = formatQuotaForForm(user.quota_bytes);
    setEditingUser({
      id: user.id,
      originalUsername: user.username,
      username: user.username,
      password: "",
      is_admin: user.is_admin,
      originalQuotaValue: quotaValue,
      quotaValue,
      used_bytes: user.used_bytes,
      reserved_bytes: user.reserved_bytes,
      available_bytes: user.available_bytes,
    });
    setEditError(null);
  }

  async function handleUpdateUser(e: FormEvent, target: EditingUser) {
    e.preventDefault();
    const form = e.currentTarget;
    setEditError(null);

    const originalUser = users.find((u) => u.id === target.id);
    if (!originalUser) {
      setEditError("用户不存在或已被删除");
      return;
    }

    if (target.username.trim() === "") {
      setEditError("请输入用户名");
      return;
    }

    const quotaEdited = target.quotaValue !== target.originalQuotaValue;
    const newQuotaBytes = quotaEdited ? parseQuotaBytes(target.quotaValue, "GB") : null;
    if (quotaEdited && newQuotaBytes === null) {
      setEditError("配额必须为正数");
      return;
    }

    const usernameChanged = target.username !== originalUser.username;
    const passwordChanged = target.password !== "";
    // C07：V2 要求「改用户名必须同时提供新密码」。按 V2 原文就地提示，不发注定被拒的请求。
    if (usernameChanged && !passwordChanged) {
      setEditError("修改用户名时必须同时提供新密码");
      return;
    }

    const changes: { username?: string; is_admin?: boolean; quota_bytes?: number } = {};
    if (usernameChanged) changes.username = target.username;
    if (target.is_admin !== originalUser.is_admin) changes.is_admin = target.is_admin;
    if (newQuotaBytes !== null && newQuotaBytes !== originalUser.quota_bytes) {
      changes.quota_bytes = newQuotaBytes;
    }

    if (Object.keys(changes).length === 0 && !passwordChanged) {
      requestModalClose(form);
      return;
    }

    try {
      const updated = await updateUser(target.id, {
        username: target.username,
        changes,
        password: passwordChanged ? target.password : null,
        // 归档参考规则（user_service.py:317）：改他人密码标记初始密码，改自己密码不标记。
        markInitialPassword: target.id !== currentUser?.id,
      });
      if (usernameChanged && updated.id === currentUser?.id) {
        await refresh();
      }
      setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
      requestModalClose(form);
      showToast("用户信息已更新", "success");
    } catch (caught) {
      setEditError(errorMessage(caught, "更新用户失败"));
    }
  }

  async function handleDeleteUser(target: UserView, trigger: Element) {
    setDeleting(true);
    try {
      await deleteUser(target.id);
      setUsers((prev) => prev.filter((u) => u.id !== target.id));
      requestModalClose(trigger);
      // C15：V2 删除是 202 异步受理，提示只宣称受理，不宣称物理删除已完成。
      showToast("删除已受理，后台清理进行中", "success");
    } catch (caught) {
      showToast(errorMessage(caught, "删除用户失败"), "error");
    } finally {
      setDeleting(false);
    }
  }

  if (!isAdminUser) return null;
  if (loading) return null;
  if (loadError) return (
    <div className="glass-frame">
      <div className="card text-danger">{loadError}</div>
    </div>
  );

  return (
    <>
      <div className="glass-frame">
        <PageHeader title="用户管理" description="管理账号、角色与存储配额" />

        <CreateUserForm
          username={username}
          password={password}
          isAdmin={isAdmin}
          quotaValue={quotaValue}
          quotaUnit={quotaUnit}
          error={error}
          onUsernameChange={setUsername}
          onPasswordChange={setPassword}
          onAdminChange={setIsAdmin}
          onQuotaValueChange={setQuotaValue}
          onQuotaUnitChange={setQuotaUnit}
          onSubmit={handleCreateUser}
        />

        <UsersTable
          users={userViews}
          currentUserId={currentUser.id}
          onEdit={openEditModal}
          onDelete={setDeletingUser}
        />
      </div>

      {editingUser && (
        <EditUserDialog
          editingUser={editingUser}
          currentUserId={currentUser.id}
          editError={editError}
          onFieldChange={(updates) => setEditingUser({ ...editingUser, ...updates })}
          onSubmit={(event) => void handleUpdateUser(event, editingUser)}
          onClose={() => setEditingUser(null)}
        />
      )}

      {deletingUser && (
        <DeleteUserDialog
          user={deletingUser}
          deleting={deleting}
          onConfirm={(trigger) => void handleDeleteUser(deletingUser, trigger)}
          onClose={() => setDeletingUser(null)}
        />
      )}
    </>
  );
}
