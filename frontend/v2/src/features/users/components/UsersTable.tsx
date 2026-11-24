import { Icon } from "@/shared/components/Icon";
import { formatAdminBytes } from "@/shared/lib/format";
import type { UserView } from "../model";
import { storageBarSegments } from "../model";

type UsersTableProps = {
  users: UserView[];
  currentUserId: number | undefined;
  onEdit: (user: UserView) => void;
  onDelete: (user: UserView) => void;
};

function StorageCell({ user }: { user: UserView }) {
  const { usedPct, reservedPct } = storageBarSegments(user);

  return (
    <>
      <div className="admin-quota-top">
        <strong>{formatAdminBytes(user.used_bytes)} / {formatAdminBytes(user.quota_bytes)}</strong>
        <span>全站 {user.machine_share_percent.toFixed(1)}%</span>
      </div>
      <div className="admin-quota-meta">
        <span>可用 {formatAdminBytes(user.available_bytes)}</span>
        <span>预留 {formatAdminBytes(user.reserved_bytes)}</span>
      </div>
      <div
        className="admin-quota-meter"
        role="meter"
        aria-label={`${user.username} 配额使用 ${user.usage_percent.toFixed(1)}%`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={user.usage_percent}
      >
        <span className="admin-quota-used" style={{ width: `${usedPct}%` }} />
        <span className="admin-quota-reserved" style={{ width: `${reservedPct}%` }} />
      </div>
    </>
  );
}

export function UsersTable({ users, currentUserId, onEdit, onDelete }: UsersTableProps) {
  const totalUsed = users.reduce((sum, user) => sum + user.used_bytes, 0);
  const totalQuota = users.reduce((sum, user) => sum + user.quota_bytes, 0);

  return (
    <section className="panel admin-table-panel" aria-label="用户列表">
      <div className="admin-table-scroll">
        <table className="admin-table admin-user-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>用户名</th>
              <th>角色</th>
              <th>存储用量 / 配额 / 可用 / 全站占比</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const current = user.id === currentUserId;
              return (
                <tr key={user.id}>
                  <td>{user.id}</td>
                  <td>
                    <div className="admin-user-name">
                      <strong>{user.username}</strong>
                      {current && <span className="admin-self-label">当前账号</span>}
                    </div>
                  </td>
                  <td>
                    <span className={`status-badge${user.is_admin ? " status-active" : ""}`}>
                      <Icon name={user.is_admin ? "shield" : "user"} className="icon-sm" />
                      {user.is_admin ? "管理员" : "普通用户"}
                    </span>
                  </td>
                  <td className="admin-quota-cell"><StorageCell user={user} /></td>
                  <td>
                    <div className="row-actions">
                      <button
                        className="icon-btn"
                        type="button"
                        onClick={() => onEdit(user)}
                        aria-label={`编辑用户 ${user.username}`}
                        title="编辑"
                      >
                        <Icon name="edit" className="icon-sm" />
                      </button>
                      {!current && (
                        <button
                          className="icon-btn danger"
                          type="button"
                          onClick={() => onDelete(user)}
                          aria-label={`删除用户 ${user.username}`}
                          title="删除"
                        >
                          <Icon name="trash" className="icon-sm" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="admin-table-footer">
        <span>共 {users.length} 个用户</span>
        <span>已使用 {formatAdminBytes(totalUsed)} · 配额合计 {formatAdminBytes(totalQuota)}</span>
      </div>
    </section>
  );
}
