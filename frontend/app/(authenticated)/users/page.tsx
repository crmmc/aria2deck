"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import type { User, UserUpdate } from "@/types";

type EditingUser = {
  id: number;
  username: string;
  password: string;
  is_admin: boolean;
  quota: number;
  quotaValue: string;
  quotaUnit: string;
};

export default function UsersPage() {
  const router = useRouter();
  const { showToast } = useToast();
  const [users, setUsers] = useState<User[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [quotaValue, setQuotaValue] = useState("100");
  const [quotaUnit, setQuotaUnit] = useState("GB");
  const [error, setError] = useState<string | null>(null);

  const [editingUser, setEditingUser] = useState<EditingUser | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

  // 删除用户弹窗状态
  const [deletingUser, setDeletingUser] = useState<User | null>(null);
  const [deleteFiles, setDeleteFiles] = useState(false);

  useEffect(() => {
    api
      .me()
      .then((me) => {
        setCurrentUser(me);
        if (!me.is_admin) {
          router.push("/tasks");
          throw new Error("Unauthorized");
        }
        return api.listUsers();
      })
      .then((data) => {
        setUsers(data);
        setLoading(false);
      })
      .catch((err) => {
        if (err.message !== "Unauthorized") {
          console.error(err);
        }
      });
  }, [router]);

  async function handleCreateUser(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const unitMultiplier: Record<string, number> = {
        KB: 1024,
        MB: 1024 * 1024,
        GB: 1024 * 1024 * 1024,
      };
      const quotaBytes = parseFloat(quotaValue) * unitMultiplier[quotaUnit];

      const newUser = await api.createUser({
        username,
        password,
        is_admin: isAdmin,
        quota: quotaBytes,
      });
      setUsers([...users, newUser]);
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      setQuotaValue("100");
      setQuotaUnit("GB");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function openDeleteModal(user: User) {
    setDeletingUser(user);
    setDeleteFiles(false);
  }

  async function handleDeleteUser() {
    if (!deletingUser) return;
    try {
      await api.deleteUser(deletingUser.id, deleteFiles);
      setUsers(users.filter((u) => u.id !== deletingUser.id));
      setDeletingUser(null);
      showToast("用户已删除", "success");
    } catch {
      showToast("删除用户失败", "error");
    }
  }

  function openEditModal(user: User) {
    let value = user.quota;
    let unit = "KB";

    if (value >= 1024 * 1024 * 1024) {
      value = value / (1024 * 1024 * 1024);
      unit = "GB";
    } else if (value >= 1024 * 1024) {
      value = value / (1024 * 1024);
      unit = "MB";
    } else {
      value = value / 1024;
      unit = "KB";
    }

    setEditingUser({
      id: user.id,
      username: user.username,
      password: "",
      is_admin: user.is_admin,
      quota: user.quota,
      quotaValue: value.toFixed(2),
      quotaUnit: unit,
    });
    setEditError(null);
  }

  async function handleUpdateUser(e: React.FormEvent) {
    e.preventDefault();
    if (!editingUser) return;

    setEditError(null);
    const updates: UserUpdate = {};

    const originalUser = users.find((u) => u.id === editingUser.id);
    if (editingUser.username !== originalUser?.username) {
      updates.username = editingUser.username;
    }
    if (editingUser.password) {
      updates.password = editingUser.password;
    }
    if (editingUser.is_admin !== originalUser?.is_admin) {
      updates.is_admin = editingUser.is_admin;
    }

    const unitMultiplier: Record<string, number> = {
      KB: 1024,
      MB: 1024 * 1024,
      GB: 1024 * 1024 * 1024,
    };
    const newQuotaBytes =
      parseFloat(editingUser.quotaValue) *
      unitMultiplier[editingUser.quotaUnit];

    if (newQuotaBytes !== originalUser?.quota) {
      updates.quota = newQuotaBytes;
    }

    if (Object.keys(updates).length === 0) {
      setEditingUser(null);
      return;
    }

    try {
      // 传入原始用户名用于密码哈希
      const updated = await api.updateUser(editingUser.id, updates, originalUser!.username);
      setUsers(users.map((u) => (u.id === updated.id ? updated : u)));
      setEditingUser(null);
    } catch (err) {
      setEditError((err as Error).message);
    }
  }

  if (loading) return null;

  return (
    <>
      <div className="glass-frame full-height animate-in">
        <div className="page-header">
          <h1 className="page-title">用户</h1>
          <p className="muted">管理系统用户</p>
        </div>

        <div className="card mb-7">
          <h3 className="mb-4">创建新用户</h3>
          <form onSubmit={handleCreateUser} className="create-user-form">
            <div className="create-user-fields">
              <div className="create-user-field">
                <label className="form-label">用户名</label>
                <input
                  className="input"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                />
              </div>
              <div className="create-user-field">
                <label className="form-label">密码</label>
                <input
                  className="input"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  autoComplete="new-password"
                />
              </div>
              <div className="create-user-field">
                <label className="form-label">存储配额</label>
                <div className="flex gap-2">
                  <input
                    className="input flex-1"
                    type="number"
                    step="0.01"
                    min="0.01"
                    value={quotaValue}
                    onChange={(e) => setQuotaValue(e.target.value)}
                    required
                  />
                  <select
                    className="input"
                    value={quotaUnit}
                    onChange={(e) => setQuotaUnit(e.target.value)}
                    style={{ width: 80 }}
                  >
                    <option value="KB">KB</option>
                    <option value="MB">MB</option>
                    <option value="GB">GB</option>
                  </select>
                </div>
              </div>
            </div>
            <div className="create-user-actions">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={isAdmin}
                  onChange={(e) => setIsAdmin(e.target.checked)}
                />
                <span className="text-base">管理员</span>
              </label>
              <button className="button" type="submit">
                创建用户
              </button>
            </div>
          </form>
          {error && (
            <p className="text-danger mt-3 text-base">{error}</p>
          )}
        </div>

        <div className="card p-0 overflow-hidden users-table-wrapper">
          <table className="table text-left">
            <thead className="table-header">
              <tr>
                <th className="table-cell">ID</th>
                <th className="table-cell">用户名</th>
                <th className="table-cell">角色</th>
                <th className="table-cell text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="table-row">
                  <td className="table-cell" data-label="ID">{u.id}</td>
                  <td className="table-cell font-medium" data-label="用户名">{u.username}</td>
                  <td className="table-cell" data-label="角色">
                    {u.is_admin ? (
                      <span className="badge active">管理员</span>
                    ) : (
                      <span className="badge">用户</span>
                    )}
                  </td>
                  <td className="table-cell text-right">
                    <div className="flex gap-2 flex-end">
                      <button
                        onClick={() => openEditModal(u)}
                        className="button secondary btn-sm"
                      >
                        编辑
                      </button>
                      {u.id !== currentUser?.id && (
                        <button
                          onClick={() => openDeleteModal(u)}
                          className="button secondary danger btn-sm"
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {editingUser && (
        <div
          className="modal-overlay"
          onClick={() => setEditingUser(null)}
        >
          <div
            className="modal-content max-w-400 animate-in"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="mb-5">编辑用户</h3>
            <form onSubmit={handleUpdateUser}>
              <div className="form-group">
                <label className="form-label">用户名</label>
                <input
                  className="input"
                  value={editingUser.username}
                  onChange={(e) =>
                    setEditingUser({ ...editingUser, username: e.target.value })
                  }
                  required
                />
              </div>
              <div className="form-group">
                <label className="form-label">
                  新密码 <span className="muted font-normal">(留空保持不变)</span>
                </label>
                <input
                  className="input"
                  type="password"
                  value={editingUser.password}
                  onChange={(e) =>
                    setEditingUser({ ...editingUser, password: e.target.value })
                  }
                  placeholder="••••••••"
                  autoComplete="new-password"
                />
              </div>
              <div className="form-group">
                <label className="form-label">存储配额</label>
                <div className="flex gap-2">
                  <input
                    className="input flex-1"
                    type="number"
                    step="0.01"
                    min="0.01"
                    value={editingUser.quotaValue}
                    onChange={(e) =>
                      setEditingUser({
                        ...editingUser,
                        quotaValue: e.target.value,
                      })
                    }
                    required
                  />
                  <select
                    className="input"
                    value={editingUser.quotaUnit}
                    onChange={(e) =>
                      setEditingUser({
                        ...editingUser,
                        quotaUnit: e.target.value,
                      })
                    }
                    style={{ width: 80 }}
                  >
                    <option value="KB">KB</option>
                    <option value="MB">MB</option>
                    <option value="GB">GB</option>
                  </select>
                </div>
              </div>
              <div className="mb-5">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={editingUser.is_admin}
                    onChange={(e) =>
                      setEditingUser({
                        ...editingUser,
                        is_admin: e.target.checked,
                      })
                    }
                    disabled={editingUser.id === currentUser?.id}
                  />
                  <span className="text-base">管理员用户</span>
                  {editingUser.id === currentUser?.id && (
                    <span className="text-xs muted">(不能修改自己的角色)</span>
                  )}
                </label>
              </div>
              {editError && (
                <p className="text-danger mb-4 text-base">{editError}</p>
              )}
              <div className="flex gap-3 flex-end">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setEditingUser(null)}
                >
                  取消
                </button>
                <button type="submit" className="button">
                  保存更改
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {deletingUser && (
        <div
          className="modal-overlay"
          onClick={() => setDeletingUser(null)}
        >
          <div
            className="modal-content max-w-400 animate-in"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="mb-4">删除用户</h3>
            <p className="mb-4">
              确定要删除用户 <strong>{deletingUser.username}</strong> 吗？
            </p>
            <p className="text-sm muted mb-4">
              将删除该用户的所有下载任务记录和打包任务记录。
            </p>
            <div className="mb-5">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={deleteFiles}
                  onChange={(e) => setDeleteFiles(e.target.checked)}
                />
                <span className="text-base">同时删除用户下载目录</span>
              </label>
              {deleteFiles && (
                <p className="text-sm text-danger mt-2">
                  警告：此操作不可恢复，用户的所有下载文件将被永久删除。
                </p>
              )}
            </div>
            <div className="flex gap-3 flex-end">
              <button
                type="button"
                className="button secondary"
                onClick={() => setDeletingUser(null)}
              >
                取消
              </button>
              <button
                type="button"
                className="button"
                style={{ background: "var(--danger)" }}
                onClick={handleDeleteUser}
              >
                删除
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
