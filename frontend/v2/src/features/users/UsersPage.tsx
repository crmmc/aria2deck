import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth, useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  Pagination,
} from "@/shared/components/ui";
import type { UserResponse } from "@/shared/api/types";
import { formatBytes } from "@/shared/lib/format";
import { passwordClientHash } from "@/shared/lib/crypto";
import { bytesToGib, createUser, deleteUser, fetchUsers, updateUser } from "./api";

const PAGE_SIZE = 20;
const DEFAULT_QUOTA_GIB = 100;
const MIN_PASSWORD_LENGTH = 6;

interface UserDraft {
  username: string;
  password: string;
  isAdmin: boolean;
  isInitialPassword: boolean;
  quotaGib: string;
  /** 编辑弹窗的重置密码（U3）：留空不改；一次性明文输入 + 显示切换。 */
  resetPassword: string;
}

function draftFrom(user: UserResponse): UserDraft {
  return {
    username: user.username,
    password: "",
    isAdmin: user.is_admin,
    isInitialPassword: user.is_initial_password,
    quotaGib: String(bytesToGib(user.quota_bytes)),
    resetPassword: "",
  };
}

type QuotaResult = { error: null; bytes: number } | { error: string; bytes: null };

function parseQuotaGib(raw: string): QuotaResult {
  if (raw.trim() === "") return { bytes: null, error: "请填写配额（GiB）" };
  const gib = Number(raw);
  if (!Number.isFinite(gib) || gib < 0) return { bytes: null, error: "配额必须是不小于 0 的数字" };
  return { bytes: Math.round(gib * 1024 ** 3), error: null };
}

export function UsersPage() {
  const { user: me } = useAuth();
  const { push } = useToast();
  const [items, setItems] = useState<UserResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<UserResponse | null>(null);
  const [deleting, setDeleting] = useState<UserResponse | null>(null);
  const [draft, setDraft] = useState<UserDraft | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showResetPassword, setShowResetPassword] = useState(false);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchUsers(page, PAGE_SIZE);
      if (seq !== requestSeq.current) return; // 丢弃过期响应。
      setItems(response.items);
      setTotal(response.total);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(caught, "用户列表加载失败"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [page]);

  useEffect(() => {
    if (me === null || !me.is_admin) return; // 非管理员跳离，不发列表请求。
    void load();
  }, [load, me]);

  if (me !== null && !me.is_admin) {
    return <Navigate to="/tasks" replace />;
  }

  const openCreate = () => {
    setFormError(null);
    setDraft({
      username: "",
      password: "",
      isAdmin: false,
      isInitialPassword: true,
      quotaGib: String(DEFAULT_QUOTA_GIB),
      resetPassword: "",
    });
    setCreating(true);
  };

  const openEdit = (target: UserResponse) => {
    setFormError(null);
    setShowResetPassword(false);
    setDraft(draftFrom(target));
    setEditing(target);
  };

  const closeDialog = () => {
    setCreating(false);
    setEditing(null);
    setDraft(null);
    setFormError(null);
    setShowResetPassword(false);
  };

  const handleCreate = async () => {
    if (draft === null || submitting) return;
    const username = draft.username.trim();
    if (username === "") {
      setFormError("请输入用户名");
      return;
    }
    if (username.length > 64) {
      setFormError("用户名最多 64 个字符");
      return;
    }
    if (draft.password.length < MIN_PASSWORD_LENGTH) {
      setFormError(`初始口令至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    const quota = parseQuotaGib(draft.quotaGib);
    if (quota.error !== null) {
      setFormError(quota.error);
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      await createUser({
        username,
        password: draft.password,
        isAdmin: draft.isAdmin,
        isInitialPassword: draft.isInitialPassword,
        quotaBytes: quota.bytes,
      });
      push("success", `用户 ${username} 已创建`);
      closeDialog();
      await load();
    } catch (caught) {
      setFormError(errorMessage(caught, "创建用户失败"));
    } finally {
      setSubmitting(false);
    }
  };

  const editChanged =
    editing !== null &&
    draft !== null &&
    (draft.username !== editing.username ||
      draft.isAdmin !== editing.is_admin ||
      draft.isInitialPassword !== editing.is_initial_password ||
      draft.resetPassword !== "" ||
      parseQuotaGib(draft.quotaGib).bytes !== editing.quota_bytes);

  const handleEdit = async () => {
    if (editing === null || draft === null || submitting) return;
    const username = draft.username.trim();
    if (username === "") {
      setFormError("请输入用户名");
      return;
    }
    const quota = parseQuotaGib(draft.quotaGib);
    if (quota.error !== null) {
      setFormError(quota.error);
      return;
    }
    if (draft.resetPassword !== "" && draft.resetPassword.length < MIN_PASSWORD_LENGTH) {
      setFormError(`重置密码至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      // 重置密码以「目标用户名」派生客户端哈希（管理员改他人同样按目标用户名）。
      const passwordHash =
        draft.resetPassword === ""
          ? undefined
          : await passwordClientHash(username, draft.resetPassword);
      await updateUser(editing.id, {
        username,
        is_admin: draft.isAdmin,
        is_initial_password: draft.isInitialPassword,
        quota_bytes: quota.bytes,
        passwordHash,
      });
      push("success", `用户 ${username} 已更新`);
      closeDialog();
      await load();
    } catch (caught) {
      setFormError(errorMessage(caught, "更新用户失败"));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async () => {
    if (deleting === null) return;
    const target = deleting;
    setDeleting(null);
    try {
      await deleteUser(target.id);
      push("success", `用户 ${target.username} 删除已受理，后台清理进行中`);
      // 删空末页回退：当前页只剩这一行时回上一页，否则原地重读。
      const maxPage = Math.max(1, Math.ceil((total - 1) / PAGE_SIZE));
      if (page > maxPage) setPage(maxPage);
      else await load();
    } catch (caught) {
      push("error", errorMessage(caught, "删除用户失败"));
    }
  };

  const updateDraft = (patch: Partial<UserDraft>) => {
    setDraft((current) => (current === null ? current : { ...current, ...patch }));
  };

  return (
    <section className="page" aria-label="用户管理">
      <header className="page-header">
        <h1>用户</h1>
        <div className="header-actions">
          <button type="button" className="primary-button" onClick={openCreate}>
            创建用户
          </button>
        </div>
      </header>

      {loading ? (
        <LoadingState />
      ) : error !== null ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : items.length === 0 ? (
        <EmptyState title="暂无用户" hint="点击右上角创建第一个用户。" />
      ) : (
        <>
          <table className="data-table" aria-label="用户列表">
            <thead>
              <tr>
                <th scope="col">用户名</th>
                <th scope="col">角色</th>
                <th scope="col">配额</th>
                <th scope="col">已用</th>
                <th scope="col">预留</th>
                <th scope="col">可用</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} data-testid={`user-row-${item.id}`}>
                  <td className="name-cell" title={item.username}>
                    {item.username}
                    {item.pending_delete && (
                      <span className="badge tone-warn password-badge">待删除</span>
                    )}
                    {item.is_initial_password && (
                      <span className="badge tone-info password-badge">初始密码</span>
                    )}
                  </td>
                  <td>{item.is_admin ? "管理员" : "普通用户"}</td>
                  <td>{formatBytes(item.quota_bytes)}</td>
                  <td>{formatBytes(item.used_bytes)}</td>
                  <td>{formatBytes(item.reserved_bytes)}</td>
                  <td>{formatBytes(item.available_bytes)}</td>
                  <td className="actions-cell">
                    <button type="button" className="link-button" onClick={() => openEdit(item)}>
                      编辑
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      disabled={me?.id === item.id}
                      title={me?.id === item.id ? "不能删除当前登录的账号" : undefined}
                      onClick={() => setDeleting(item)}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
        </>
      )}

      {creating && draft !== null && (
        <Modal
          title="创建用户"
          onClose={closeDialog}
          footer={
            <>
              <button type="button" className="secondary-button" onClick={closeDialog}>
                取消
              </button>
              <button
                type="button"
                className="primary-button"
                disabled={submitting}
                onClick={() => void handleCreate()}
              >
                {submitting ? "创建中…" : "创建"}
              </button>
            </>
          }
        >
          {formError !== null && (
            <div className="form-error" role="alert">
              {formError}
            </div>
          )}
          <label className="form-field">
            <span>用户名</span>
            <input
              value={draft.username}
              onChange={(event) => updateDraft({ username: event.target.value })}
              maxLength={64}
            />
          </label>
          <label className="form-field">
            <span>初始口令（至少 6 位）</span>
            <input
              type="password"
              value={draft.password}
              autoComplete="new-password"
              onChange={(event) => updateDraft({ password: event.target.value })}
            />
          </label>
          <label className="form-field">
            <span>配额（GiB）</span>
            <input
              value={draft.quotaGib}
              onChange={(event) => updateDraft({ quotaGib: event.target.value })}
              inputMode="decimal"
            />
          </label>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={draft.isAdmin}
              onChange={(event) => updateDraft({ isAdmin: event.target.checked })}
            />
            <span>设为管理员</span>
          </label>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={draft.isInitialPassword}
              onChange={(event) => updateDraft({ isInitialPassword: event.target.checked })}
            />
            <span>标记为初始密码（用户首次修改免旧密码）</span>
          </label>
        </Modal>
      )}

      {editing !== null && draft !== null && (
        <Modal
          title={`编辑用户：${editing.username}`}
          onClose={closeDialog}
          footer={
            <>
              <button type="button" className="secondary-button" onClick={closeDialog}>
                取消
              </button>
              <button
                type="button"
                className="primary-button"
                disabled={!editChanged || submitting}
                onClick={() => void handleEdit()}
              >
                {submitting ? "保存中…" : "保存"}
              </button>
            </>
          }
        >
          {formError !== null && (
            <div className="form-error" role="alert">
              {formError}
            </div>
          )}
          <label className="form-field">
            <span>用户名</span>
            <input
              value={draft.username}
              onChange={(event) => updateDraft({ username: event.target.value })}
              maxLength={64}
            />
          </label>
          <label className="form-field">
            <span>配额（GiB，当前 {formatBytes(editing.quota_bytes)}）</span>
            <input
              value={draft.quotaGib}
              onChange={(event) => updateDraft({ quotaGib: event.target.value })}
              inputMode="decimal"
            />
          </label>
          <label className="form-field">
            <span>重置密码（可选，留空不修改，至少 {MIN_PASSWORD_LENGTH} 位）</span>
            <span className="password-row">
              <input
                type={showResetPassword ? "text" : "password"}
                value={draft.resetPassword}
                autoComplete="new-password"
                onChange={(event) => updateDraft({ resetPassword: event.target.value })}
                aria-label="重置密码"
              />
              <button
                type="button"
                className="password-toggle"
                aria-label={showResetPassword ? "隐藏重置密码" : "显示重置密码"}
                aria-pressed={showResetPassword}
                onClick={() => setShowResetPassword((current) => !current)}
              >
                {showResetPassword ? "隐藏" : "显示"}
              </button>
            </span>
          </label>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={draft.isAdmin}
              disabled={me?.id === editing.id}
              onChange={(event) => updateDraft({ isAdmin: event.target.checked })}
            />
            <span>管理员{me?.id === editing.id ? "（不能修改自己的角色）" : ""}</span>
          </label>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={draft.isInitialPassword}
              onChange={(event) => updateDraft({ isInitialPassword: event.target.checked })}
            />
            <span>标记为初始密码</span>
          </label>
          <p className="hint-text">未修改任何内容时，保存不可用，关闭对话框不会提交。</p>
        </Modal>
      )}

      {deleting !== null && (
        <ConfirmDialog
          title="删除用户"
          message={`确定删除用户 ${deleting.username}？删除已受理后将撤销其全部凭证并在后台清理其数据，该过程不可恢复。`}
          confirmLabel="删除"
          danger
          onConfirm={() => void handleDelete()}
          onCancel={() => setDeleting(null)}
        />
      )}
    </section>
  );
}
