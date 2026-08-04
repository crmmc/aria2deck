"use client";

import { createContext, useContext, useState, useCallback, useEffect, useMemo, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMounted } from "@/lib/useMounted";
import { ModalOverlay } from "@/components/ModalOverlay";

type ToastType = "success" | "error" | "info" | "warning";

interface Toast {
  id: number;
  message: string;
  type: ToastType;
}

interface ToastContextType {
  showToast: (message: string, type?: ToastType) => void;
  showConfirm: (options: ConfirmOptions) => Promise<boolean>;
}

interface ConfirmOptions {
  title?: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

interface ConfirmState extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

const ToastContext = createContext<ToastContextType | null>(null);

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used within ToastProvider");
  }
  return context;
}

let toastId = 0;

function getToastClass(type: ToastType): string {
  switch (type) {
    case "success": return "toast-success";
    case "error": return "toast-error";
    case "warning": return "toast-warning";
    default: return "toast-info";
  }
}

function getToastIcon(type: ToastType): string {
  switch (type) {
    case "success": return "✓";
    case "error": return "✕";
    case "warning": return "⚠";
    default: return "ℹ";
  }
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const mounted = useMounted();
  const toastTimersRef = useRef<Set<ReturnType<typeof setTimeout>> | null>(null);
  if (toastTimersRef.current === null) {
    toastTimersRef.current = new Set();
  }
  const toastTimers = toastTimersRef.current;
  const confirmRef = useRef<ConfirmState | null>(null);
  const confirmResolversRef = useRef<Set<ConfirmState["resolve"]> | null>(null);
  if (confirmResolversRef.current === null) {
    confirmResolversRef.current = new Set();
  }
  const confirmResolvers = confirmResolversRef.current;

  useEffect(() => {
    return () => {
      toastTimers.forEach(clearTimeout);
      confirmResolvers.forEach((resolve) => resolve(false));
    };
  }, [toastTimers, confirmResolvers]);

  const showToast = useCallback((message: string, type: ToastType = "info") => {
    const id = ++toastId;
    setToasts((prev) => [...prev, { id, message, type }]);
    const timer = setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      toastTimers.delete(timer);
    }, 3000);
    toastTimers.add(timer);
  }, [toastTimers]);

  const showConfirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      const state = { ...options, resolve };
      confirmRef.current = state;
      confirmResolvers.add(resolve);
      setConfirm(state);
    });
  }, [confirmResolvers]);

  const handleConfirm = useCallback((result: boolean) => {
    const current = confirmRef.current;
    current?.resolve(result);
    if (current) {
      confirmResolvers.delete(current.resolve);
    }
    confirmRef.current = null;
    setConfirm(null);
  }, [confirmResolvers]);

  const contextValue = useMemo(
    () => ({ showToast, showConfirm }),
    [showToast, showConfirm]
  );

  return (
    <ToastContext.Provider value={contextValue}>
      {children}
      {typeof window !== "undefined" &&
        mounted &&
        createPortal(
          <>
            <div className="toast-container">
              {toasts.map((toast) => (
                <div key={toast.id} className={`toast-item ${getToastClass(toast.type)}`}>
                  <span className="toast-icon">{getToastIcon(toast.type)}</span>
                  <span>{toast.message}</span>
                </div>
              ))}
            </div>

            {confirm && (
              <ModalOverlay
                onClose={() => handleConfirm(false)}
                ariaLabel={confirm.title || "确认操作"}
                className="confirm-overlay"
                contentClassName="confirm-content"
              >
                  {confirm.title && (
                    <h3 className="confirm-title">{confirm.title}</h3>
                  )}
                  <p className="confirm-message">{confirm.message}</p>
                  <div className="flex gap-3 flex-end">
                    <button type="button"
                      className="button secondary"
                      onClick={() => handleConfirm(false)}
                    >
                      {confirm.cancelText || "取消"}
                    </button>
                    <button type="button"
                      className="button"
                      style={confirm.danger ? { background: "var(--danger)" } : undefined}
                      onClick={() => handleConfirm(true)}
                    >
                      {confirm.confirmText || "确定"}
                    </button>
                  </div>
              </ModalOverlay>
            )}

            <style>{`
              @keyframes slideIn {
                from {
                  opacity: 0;
                  transform: translateX(20px);
                }
                to {
                  opacity: 1;
                  transform: translateX(0);
                }
              }
              @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
              }
              @keyframes scaleIn {
                from {
                  opacity: 0;
                  transform: scale(0.95);
                }
                to {
                  opacity: 1;
                  transform: scale(1);
                }
              }
            `}</style>
          </>,
          document.body
        )}
    </ToastContext.Provider>
  );
}
