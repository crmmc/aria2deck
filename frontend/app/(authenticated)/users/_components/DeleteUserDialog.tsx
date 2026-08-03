import { ModalOverlay } from "@/components/ModalOverlay";
import type { User } from "@/types";

type DeleteUserDialogProps = {
  user: User;
  onConfirm: () => void;
  onClose: () => void;
};

export function DeleteUserDialog({ user, onConfirm, onClose }: DeleteUserDialogProps) {
  return (
    <ModalOverlay
      onClose={onClose}
      ariaLabel="删除用户"
      contentClassName="modal-content max-w-400 animate-in"
    >
        <h3 className="mb-4">删除用户</h3>
        <p className="mb-4">
          确定要删除用户 <strong>{user.username}</strong> 吗？
        </p>
        <p className="text-sm muted mb-4">
          将删除该用户的所有下载任务记录、打包任务记录和文件引用。
        </p>
        <div className="flex gap-3 flex-end">
          <button
            type="button"
            className="button secondary"
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="button"
            className="button"
            style={{ background: "var(--danger)" }}
            onClick={onConfirm}
          >
            删除
          </button>
        </div>
    </ModalOverlay>
  );
}
