import type { User } from "@/types";

type UsersTableProps = {
  users: User[];
  currentUserId: number | undefined;
  onEdit: (user: User) => void;
  onDelete: (user: User) => void;
};

export function UsersTable({ users, currentUserId, onEdit, onDelete }: UsersTableProps) {
  return (
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
