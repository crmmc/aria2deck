import { useEffect, useRef, type ReactNode } from "react";

/** 通用 UI：Modal（Escape/背景关闭/焦点管理与陷阱）、ConfirmDialog、Pagination、EmptyState、StatusBadge、ToastHost。 */

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = ref.current;
      if (panel === null) return;
      // 焦点陷阱：Tab/Shift+Tab 始终在对话框内的可聚焦元素间循环。
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const active = document.activeElement as HTMLElement | null;
      const currentIndex =
        active !== null && panel.contains(active) ? focusable.indexOf(active) : -1;
      event.preventDefault();
      const nextIndex = event.shiftKey
        ? currentIndex <= 0
          ? focusable.length - 1
          : currentIndex - 1
        : currentIndex === -1 || currentIndex === focusable.length - 1
          ? 0
          : currentIndex + 1;
      focusable[nextIndex].focus();
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      previous?.focus();
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="modal-panel"
        tabIndex={-1}
      >
        <header className="modal-header">
          <h2>{title}</h2>
          <button type="button" className="modal-close" aria-label="关闭" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer !== undefined && <footer className="modal-footer">{footer}</footer>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "确认",
  danger = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal
      title={title}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className={danger ? "danger-button" : "primary-button"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      <p>{message}</p>
    </Modal>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <nav className="pagination" aria-label="分页">
      <button
        type="button"
        disabled={page <= 1}
        onClick={() => onChange(page - 1)}
        aria-label="上一页"
      >
        上一页
      </button>
      <span aria-live="polite">
        第 {page} / {pages} 页 · 共 {total} 条
      </span>
      <button
        type="button"
        disabled={page >= pages}
        onClick={() => onChange(page + 1)}
        aria-label="下一页"
      >
        下一页
      </button>
    </nav>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      {hint !== undefined && <p>{hint}</p>}
    </div>
  );
}

export function LoadingState({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="loading-state" aria-busy="true">
      {label}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <p>{message}</p>
      {onRetry !== undefined && (
        <button type="button" className="secondary-button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  active: "ok",
  waiting: "info",
  queued: "info",
  paused: "warn",
  completed: "ok",
  failed: "error",
  cancelled: "muted",
  expired: "muted",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return (
    <span className={`badge tone-${STATUS_TONE[status] ?? "muted"}`}>
      {label ?? status}
    </span>
  );
}
