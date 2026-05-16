"use client";

import { createContext, useContext, useState, useCallback, useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
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

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [mounted, setMounted] = useState(false);
  const toastTimers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const confirmRef = useRef<ConfirmState | null>(null);

  useEffect(() => {
    setMounted(true);
    return () => {
      toastTimers.current.forEach(clearTimeout);
      confirmRef.current?.resolve(false);
    };
  }, []);

  const showToast = useCallback((message: string, type: ToastType = "info") => {
    const id = ++toastId;
    setToasts((prev) => [...prev, { id, message, type }]);
    const timer = setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      toastTimers.current.delete(timer);
    }, 3000);
    toastTimers.current.add(timer);
  }, []);

  const showConfirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      const state = { ...options, resolve };
      confirmRef.current = state;
      setConfirm(state);
    });
  }, []);

  const handleConfirm = useCallback((result: boolean) => {
    confirmRef.current?.resolve(result);
    confirmRef.current = null;
    setConfirm(null);
  }, []);

  const getToastClass = (type: ToastType) => {
    switch (type) {
      case "success": return "toast-success";
      case "error": return "toast-error";
      case "warning": return "toast-warning";
      default: return "toast-info";
    }
  };

  const getToastIcon = (type: ToastType) => {
    switch (type) {
      case "success": return "✓";
      case "error": return "✕";
      case "warning": return "⚠";
      default: return "ℹ";
    }
  };

  return (
    <ToastContext.Provider value={{ showToast, showConfirm }}>
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
                className="confirm-overlay"
                contentClassName="confirm-content"
              >
                  {confirm.title && (
                    <h3 className="confirm-title">{confirm.title}</h3>
                  )}
                  <p className="confirm-message">{confirm.message}</p>
                  <div className="flex gap-3 flex-end">
                    <button
                      className="button secondary"
                      onClick={() => handleConfirm(false)}
                    >
                      {confirm.cancelText || "取消"}
                    </button>
                    <button
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
