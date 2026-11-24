"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import type { User, UserUpdate } from "@/types";
import { parseQuotaBytes, formatQuotaForForm } from "./userState";
import { CreateUserForm } from "./_components/CreateUserForm";
import { UsersTable } from "./_components/UsersTable";
import { EditUserDialog, type EditingUser } from "./_components/EditUserDialog";
import { DeleteUserDialog } from "./_components/DeleteUserDialog";

type InitialLoadState = {
  currentUser: User | null;
  loading: boolean;
  error: string | null;
};

export default function UsersPage() {
  const { push } = useRouter();
  const { showToast } = useToast();
  const [users, setUsers] = useState<User[]>([]);
  const [initialLoad, setInitialLoad] = useState<InitialLoadState>({
    currentUser: null,
    loading: true,
    error: null,
  });

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [quotaValue, setQuotaValue] = useState("100");
  const [quotaUnit, setQuotaUnit] = useState("GB");
  const [error, setError] = useState<string | null>(null);

  const [editingUser, setEditingUser] = useState<EditingUser | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  const [deletingUser, setDeletingUser] = useState<User | null>(null);

  useEffect(() => {
    let mounted = true;
    (async () => {
      let me: User | null = null;
      try {
        me = await api.me();
        if (!mounted) return;

        if (!me.is_admin) {
          setInitialLoad({ currentUser: me, loading: false, error: null });
          push("/tasks");
          return;
        }

        const data = await api.listUsers();
        if (!mounted) return;
        setUsers(data);
        setInitialLoad({ currentUser: me, loading: false, error: null });
      } catch (err) {
        if (!mounted) return;
        console.error(err);
        setInitialLoad({
          currentUser: me,
          loading: false,
          error: "加载用户列表失败",
        });
      }
    })();
    return () => {
      mounted = false;
    };
  }, [push]);

  async function handleCreateUser(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const quotaBytes = parseQuotaBytes(quotaValue, quotaUnit);
    if (quotaBytes === null) {
      setError("配额必须为正数");
      return;
    }

    try {
      const newUser = await api.createUser({
        username,
        password,
        is_admin: isAdmin,
        quota: quotaBytes,
      });
      setUsers((prev) => [...prev, newUser]);
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      setQuotaValue("100");
      setQuotaUnit("GB");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function openEditModal(user: User) {
    const { quotaValue: qv, quotaUnit: qu } = formatQuotaForForm(user.quota);
    setEditingUser({
      id: user.id,
      username: user.username,
      password: "",
      is_admin: user.is_admin,
      quota: user.quota,
      quotaValue: qv,
      quotaUnit: qu,
      used_bytes: user.used_bytes,
      reserved_bytes: user.reserved_bytes,
      available_bytes: user.available_bytes,
      usage_percent: user.usage_percent,
      machine_share_percent: user.machine_share_percent,
    });
    setEditError(null);
  }

  async function handleUpdateUser(e: React.FormEvent) {
    e.preventDefault();
    if (!editingUser) return;

    setEditError(null);
    const updates: UserUpdate = {};

    const originalUser = users.find((u) => u.id === editingUser.id);
    if (!originalUser) {
      setEditError("用户不存在或已被删除");
      return;
    }
    if (editingUser.username !== originalUser.username) {
      updates.username = editingUser.username;
    }
    if (editingUser.password) {
      updates.password = editingUser.password;
    }
    if (editingUser.is_admin !== originalUser.is_admin) {
      updates.is_admin = editingUser.is_admin;
    }

    const newQuotaBytes = parseQuotaBytes(editingUser.quotaValue, editingUser.quotaUnit);
    if (newQuotaBytes === null) {
      setEditError("配额必须为正数");
      return;
    }

    if (newQuotaBytes !== originalUser.quota) {
      updates.quota = newQuotaBytes;
    }

    if (Object.keys(updates).length === 0) {
      setEditingUser(null);
      return;
    }

    try {
      const updated = await api.updateUser(editingUser.id, updates, originalUser.username);
      setUsers((prev) => prev.map((u) => (u.id === updated.id ? updated : u)));
      setEditingUser(null);
    } catch (err) {
      setEditError((err as Error).message);
    }
  }

  async function handleDeleteUser() {
    if (!deletingUser) return;
    try {
      await api.deleteUser(deletingUser.id);
      setUsers((prev) => prev.filter((u) => u.id !== deletingUser.id));
      setDeletingUser(null);
      showToast("用户已删除", "success");
    } catch {
      showToast("删除用户失败", "error");
    }
  }

  if (initialLoad.loading) return null;
  if (initialLoad.error) return (
    <div className="glass-frame full-height animate-in">
      <div className="card text-danger">{initialLoad.error}</div>
    </div>
  );
  if (!initialLoad.currentUser?.is_admin) return null;

  return (
    <>
      <div className="glass-frame full-height animate-in">
        <div className="page-header">
          <h1 className="page-title">用户</h1>
          <p className="muted">管理系统用户</p>
        </div>

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
          users={users}
          currentUserId={initialLoad.currentUser?.id}
          onEdit={openEditModal}
          onDelete={setDeletingUser}
        />
      </div>

      {editingUser && (
        <EditUserDialog
          editingUser={editingUser}
          currentUserId={initialLoad.currentUser?.id}
          editError={editError}
          onFieldChange={(updates) =>
            setEditingUser((prev) => prev ? { ...prev, ...updates } : prev)
          }
          onSubmit={handleUpdateUser}
          onClose={() => setEditingUser(null)}
        />
      )}

      {deletingUser && (
        <DeleteUserDialog
          user={deletingUser}
          onConfirm={handleDeleteUser}
          onClose={() => setDeletingUser(null)}
        />
      )}
    </>
  );
}
