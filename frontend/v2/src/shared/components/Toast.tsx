"use client";

import { useState, useCallback, useEffect, useMemo, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMounted } from "@/shared/hooks/useMounted";
import { AnimatedToast } from "@/shared/components/AnimatedToast";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { requestModalClose } from "@/shared/ui/modalClose";
import {
  NotificationStackItem,
  NotificationStackProvider,
} from "@/shared/components/NotificationStack";
import {
  ToastContext,
  bindToastEmitter,
  type ConfirmOptions,
  type ConfirmState,
  type ToastType,
} from "@/shared/components/toast-context";

interface Toast {
  id: number;
  message: string;
  type: ToastType;
}

let toastId = 0;

/** 同时可见上限：多出的排队，等有空位再入场（此时才开始计时）。 */
const MAX_VISIBLE_TOASTS = 5;

function getToastClass(type: ToastType): string {
  switch (type) {
    case "success": return "toast-success";
    case "error": return "toast-error";
    case "warning": return "toast-warning";
    default: return "toast-info";
  }
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const mounted = useMounted();
  const confirmRef = useRef<ConfirmState | null>(null);
  const confirmResultRef = useRef(false);
  const confirmResolversRef = useRef<Set<ConfirmState["resolve"]> | null>(null);
  if (confirmResolversRef.current === null) {
    confirmResolversRef.current = new Set();
  }
  const confirmResolvers = confirmResolversRef.current;

  useEffect(() => {
    return () => confirmResolvers.forEach((resolve) => resolve(false));
  }, [confirmResolvers]);

  const showToast = useCallback((message: string, type: ToastType = "info") => {
    const id = ++toastId;
    setToasts((prev) => [...prev, { id, message, type }]);
  }, []);

  // 绑定模块级发射桥，供 providers 初始化等非 React 上下文弹 归档参考 样式 toast。
  useEffect(() => {
    bindToastEmitter(showToast);
    return () => bindToastEmitter(null);
  }, [showToast]);

  const showConfirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      const state = { ...options, resolve };
      confirmRef.current = state;
      confirmResultRef.current = false;
      confirmResolvers.add(resolve);
      setConfirm(state);
    });
  }, [confirmResolvers]);

  const handleConfirm = useCallback((result: boolean) => {
    const current = confirmRef.current;
    if (!current) return;
    confirmRef.current = null;
    confirmResolvers.delete(current.resolve);
    setConfirm(null);
    current.resolve(result);
  }, [confirmResolvers]);

  const contextValue = useMemo(
    () => ({ showToast, showConfirm }),
    [showToast, showConfirm],
  );

  const visibleToasts = toasts.slice(0, MAX_VISIBLE_TOASTS);

  return (
    <NotificationStackProvider>
      <ToastContext.Provider value={contextValue}>
        {children}
        {typeof window !== "undefined" &&
          mounted && (
            <>
              {visibleToasts.map((toast, index) => (
                <NotificationStackItem
                  key={toast.id}
                  order={1 + visibleToasts.length - 1 - index}
                >
                  <AnimatedToast
                    className={`toast-item ${getToastClass(toast.type)}`}
                    message={toast.message}
                    type={toast.type}
                    duration={3000}
                    onDismiss={() => setToasts((current) => current.filter((item) => item.id !== toast.id))}
                  />
                </NotificationStackItem>
              ))}

            {confirm && createPortal(
              <ModalOverlay
                onClose={() => handleConfirm(confirmResultRef.current)}
                ariaLabel={confirm.title || "确认操作"}
                className="confirm-overlay"
                contentClassName="confirm-content"
              >
                {confirm.title && (
                  <h3 className="confirm-title">{confirm.title}</h3>
                )}
                <p className="confirm-message">{confirm.message}</p>
                <div className="flex gap-3 flex-end">
                  <button
                    type="button"
                    className="button secondary"
                    onClick={(event) => {
                      confirmResultRef.current = false;
                      requestModalClose(event.currentTarget);
                    }}
                  >
                    {confirm.cancelText || "取消"}
                  </button>
                  <button
                    type="button"
                    className="button"
                    style={confirm.danger ? { background: "var(--danger)" } : undefined}
                    onClick={(event) => {
                      confirmResultRef.current = true;
                      requestModalClose(event.currentTarget);
                    }}
                  >
                    {confirm.confirmText || "确定"}
                  </button>
                </div>
              </ModalOverlay>,
              document.body,
            )}
          </>
        )}
      </ToastContext.Provider>
    </NotificationStackProvider>
  );
}
