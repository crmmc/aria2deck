import { formatBytes } from "@/shared/lib/format";
import type { UserView } from "../model";
import { storageBarSegments, usageColor } from "../model";

type UsersTableProps = {
  users: UserView[];
  currentUserId: number | undefined;
  onEdit: (user: UserView) => void;
  onDelete: (user: UserView) => void;
};

function StorageCell({ user }: { user: UserView }) {
  const { usedPct, reservedPct } = storageBarSegments(user);
  const usagePercent = user.usage_percent;
  const reserved = user.reserved_bytes;

  return (
    <div className="user-storage-cell">
      <div className="user-storage-meta">
        <span className="font-medium">
          {formatBytes(user.used_bytes)} / {formatBytes(user.quota_bytes)}
        </span>
        <span className="muted text-xs">{usagePercent.toFixed(1)}%</span>
      </div>
      <div className="progress-container user-storage-bar" aria-label={`存储占用 ${usagePercent}%`}>
        <div
          className="progress-bar"
          style={{
            width: `${usedPct + reservedPct}%`,
            background: usageColor(usagePercent),
          }}
        />
        {reserved > 0 && (
          <div
            className="user-storage-reserved"
            style={{
              left: `${usedPct}%`,
              width: `${reservedPct}%`,
            }}
          />
        )}
      </div>
      <div className="user-storage-sub muted text-xs">
        可用 {formatBytes(user.available_bytes)}
        {reserved > 0 ? ` · 冻结 ${formatBytes(reserved)}` : ""}
        {` · 全站 ${user.machine_share_percent.toFixed(1)}%`}
      </div>
    </div>
  );
}

export function UsersTable({ users, currentUserId, onEdit, onDelete }: UsersTableProps) {
  return (
    <div className="card p-0 overflow-hidden users-table-wrapper">
      <table className="table text-left">
        <thead className="table-header">
          <tr>
            <th className="table-cell">ID</th>
            <th className="table-cell">用户名</th>
            <th className="table-cell">角色</th>
            <th className="table-cell">存储</th>
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
              <td className="table-cell" data-label="存储">
                <StorageCell user={u} />
              </td>
              <td className="table-cell text-right">
                <div className="flex gap-2 flex-end">
                  <button
                    type="button"
                    onClick={() => onEdit(u)}
                    className="button secondary btn-sm"
                  >
                    编辑
                  </button>
                  {u.id !== currentUserId && (
                    <button
                      type="button"
                      onClick={() => onDelete(u)}
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
  );
}
