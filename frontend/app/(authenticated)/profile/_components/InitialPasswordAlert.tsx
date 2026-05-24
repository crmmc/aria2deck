import { createPortal } from "react-dom";

type InitialPasswordAlertProps = {
  open: boolean;
  onClose: () => void;
};

export function InitialPasswordAlert({ open, onClose }: InitialPasswordAlertProps) {
  if (!open) return null;

  return createPortal(
    <div className="modal-overlay z-10000">
      <div className="modal-content max-w-400 animate-in">
        <div className="alert alert-danger mb-4">
          <p className="text-center m-0" style={{ fontSize: '15px' }}>
            您当前使用的是管理员设置的初始密码，为了账户安全，请尽快修改为您自己的密码。
          </p>
        </div>
        <button
          type="button"
          className="button w-full"
          onClick={onClose}
        >
          我知道了
        </button>
      </div>
    </div>,
    document.body
  );
}
