"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import AuthLayout from "@/components/AuthLayout";
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
  const [users, setUsers] = useState<User[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Create form state
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [quotaValue, setQuotaValue] = useState("100");
  const [quotaUnit, setQuotaUnit] = useState("GB");
  const [error, setError] = useState<string | null>(null);

  // Edit modal state
  const [editingUser, setEditingUser] = useState<EditingUser | null>(null);
  const [editError, setEditError] = useState<string | null>(null);

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
      // 转换配额为字节
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

  async function handleDeleteUser(id: number) {
    if (!confirm("确定要删除此用户吗？")) return;
    try {
      await api.deleteUser(id);
      setUsers(users.filter((u) => u.id !== id));
    } catch (err) {
      alert("删除用户失败");
    }
  }

  function openEditModal(user: User) {
    // 将字节转换为合适的单位
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

    // 计算新的配额（字节）
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
      const updated = await api.updateUser(editingUser.id, updates);
      setUsers(users.map((u) => (u.id === updated.id ? updated : u)));
      setEditingUser(null);
    } catch (err) {
      setEditError((err as Error).message);
    }
  }

  if (loading) return null;

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <h1 style={{ marginBottom: 8 }}>用户</h1>
        <p className="muted" style={{ marginBottom: 32 }}>
          管理系统用户
        </p>

        <div className="card" style={{ marginBottom: 32 }}>
          <h3 style={{ marginBottom: 16 }}>创建新用户</h3>
          <form
            onSubmit={handleCreateUser}
            style={{
              display: "flex",
              gap: 12,
              alignItems: "flex-end",
              flexWrap: "wrap",
            }}
          >
            <div style={{ flex: 1, minWidth: 200 }}>
              <label
                style={{
                  display: "block",
                  fontSize: 13,
                  marginBottom: 4,
                  fontWeight: 500,
                }}
              >
                用户名
              </label>
              <input
                className="input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>
            <div style={{ flex: 1, minWidth: 200 }}>
              <label
                style={{
                  display: "block",
                  fontSize: 13,
                  marginBottom: 4,
                  fontWeight: 500,
                }}
              >
                密码
              </label>
              <input
                className="input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="new-password"
              />
            </div>
            <div style={{ flex: 1, minWidth: 200 }}>
              <label
                style={{
                  display: "block",
                  fontSize: 13,
                  marginBottom: 4,
                  fontWeight: 500,
                }}
              >
                存储配额
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <input
                  className="input"
                  type="number"
                  step="0.01"
                  min="0.01"
                  value={quotaValue}
                  onChange={(e) => setQuotaValue(e.target.value)}
                  required
                  style={{ flex: 1 }}
                />
                <select
                  className="input"
                  value={quotaUnit}
                  onChange={(e) => setQuotaUnit(e.target.value)}
                  style={{ width: "80px" }}
                >
                  <option value="KB">KB</option>
                  <option value="MB">MB</option>
                  <option value="GB">GB</option>
                </select>
              </div>
            </div>
            <div style={{ paddingBottom: 12 }}>
              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={isAdmin}
                  onChange={(e) => setIsAdmin(e.target.checked)}
                />
                <span style={{ fontSize: 14 }}>管理员用户</span>
              </label>
            </div>
            <button className="button" type="submit">
              创建用户
            </button>
          </form>
          {error && (
            <p style={{ color: "var(--danger)", marginTop: 12, fontSize: 14 }}>
              {error}
            </p>
          )}
        </div>

        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              textAlign: "left",
            }}
          >
            <thead
              style={{
                background: "rgba(0,0,0,0.02)",
                borderBottom: "1px solid rgba(0,0,0,0.05)",
              }}
            >
              <tr>
                <th
                  style={{
                    padding: "12px 16px",
                    fontWeight: 600,
                    color: "var(--muted)",
                    fontSize: 13,
                  }}
                >
                  ID
                </th>
                <th
                  style={{
                    padding: "12px 16px",
                    fontWeight: 600,
                    color: "var(--muted)",
                    fontSize: 13,
                  }}
                >
                  用户名
                </th>
                <th
                  style={{
                    padding: "12px 16px",
                    fontWeight: 600,
                    color: "var(--muted)",
                    fontSize: 13,
                  }}
                >
                  角色
                </th>
                <th
                  style={{
                    padding: "12px 16px",
                    fontWeight: 600,
                    color: "var(--muted)",
                    fontSize: 13,
                    textAlign: "right",
                  }}
                >
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr
                  key={u.id}
                  style={{ borderBottom: "1px solid rgba(0,0,0,0.05)" }}
                >
                  <td style={{ padding: "12px 16px" }}>{u.id}</td>
                  <td style={{ padding: "12px 16px", fontWeight: 500 }}>
                    {u.username}
                  </td>
                  <td style={{ padding: "12px 16px" }}>
                    {u.is_admin ? (
                      <span className="badge active">管理员</span>
                    ) : (
                      <span className="badge">用户</span>
                    )}
                  </td>
                  <td style={{ padding: "12px 16px", textAlign: "right" }}>
                    <div
                      style={{
                        display: "flex",
                        gap: 8,
                        justifyContent: "flex-end",
                      }}
                    >
                      <button
                        onClick={() => openEditModal(u)}
                        className="button secondary"
                        style={{
                          padding: "4px 12px",
                          fontSize: 12,
                          height: 28,
                        }}
                      >
                        编辑
                      </button>
                      {u.id !== currentUser?.id && (
                        <button
                          onClick={() => handleDeleteUser(u.id)}
                          className="button secondary danger"
                          style={{
                            padding: "4px 12px",
                            fontSize: 12,
                            height: 28,
                          }}
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

      {/* Edit Modal */}
      {editingUser && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={() => setEditingUser(null)}
        >
          <div
            className="card"
            style={{
              width: "100%",
              maxWidth: 400,
              margin: 16,
              animation: "fadeIn 0.2s ease",
              background: "#fff",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 style={{ marginBottom: 20 }}>编辑用户</h3>
            <form onSubmit={handleUpdateUser}>
              <div style={{ marginBottom: 16 }}>
                <label
                  style={{
                    display: "block",
                    fontSize: 13,
                    marginBottom: 4,
                    fontWeight: 500,
                  }}
                >
                  用户名
                </label>
                <input
                  className="input"
                  value={editingUser.username}
                  onChange={(e) =>
                    setEditingUser({ ...editingUser, username: e.target.value })
                  }
                  required
                />
              </div>
              <div style={{ marginBottom: 16 }}>
                <label
                  style={{
                    display: "block",
                    fontSize: 13,
                    marginBottom: 4,
                    fontWeight: 500,
                  }}
                >
                  新密码{" "}
                  <span style={{ color: "var(--muted)", fontWeight: 400 }}>
                    (留空保持不变)
                  </span>
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
              <div style={{ marginBottom: 16 }}>
                <label
                  style={{
                    display: "block",
                    fontSize: 13,
                    marginBottom: 4,
                    fontWeight: 500,
                  }}
                >
                  存储配额
                </label>
                <div style={{ display: "flex", gap: 8 }}>
                  <input
                    className="input"
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
                    style={{ flex: 1 }}
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
                    style={{ width: "80px" }}
                  >
                    <option value="KB">KB</option>
                    <option value="MB">MB</option>
                    <option value="GB">GB</option>
                  </select>
                </div>
              </div>
              <div style={{ marginBottom: 20 }}>
                <label
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                  }}
                >
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
                  <span style={{ fontSize: 14 }}>管理员用户</span>
                  {editingUser.id === currentUser?.id && (
                    <span style={{ fontSize: 12, color: "var(--muted)" }}>
                      (不能修改自己的角色)
                    </span>
                  )}
                </label>
              </div>
              {editError && (
                <p
                  style={{
                    color: "var(--danger)",
                    marginBottom: 16,
                    fontSize: 14,
                  }}
                >
                  {editError}
                </p>
              )}
              <div
                style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}
              >
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
    </AuthLayout>
  );
}
