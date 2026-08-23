"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { User } from "@/types";

const POLL_INTERVAL_MS = 20_000;

type BackendStatus = {
  status: "ok" | "degraded";
  message: string;
};

type Props = {
  user: User | null;
};

export default function BackendStatusBanner({ user }: Props) {
  const { push } = useRouter();
  const [backend, setBackend] = useState<BackendStatus | null>(null);
  const [dismissedWhileDegraded, setDismissedWhileDegraded] = useState(false);
  const mountedRef = useRef(true);
  const previousStatusRef = useRef<BackendStatus["status"] | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const result = await api.getSystemStatus();
      if (!mountedRef.current) return;
      const next = result.download_backend;
      if (
        previousStatusRef.current === "degraded" &&
        next.status === "ok"
      ) {
        setDismissedWhileDegraded(false);
      }
      previousStatusRef.current = next.status;
      setBackend(next);
    } catch {
      // Keep the last known status; a failed status poll should not itself
      // claim the download backend is down.
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadStatus();

    const intervalId = window.setInterval(() => {
      void loadStatus();
    }, POLL_INTERVAL_MS);

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void loadStatus();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      mountedRef.current = false;
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [loadStatus]);

  if (!user || !backend || backend.status !== "degraded" || dismissedWhileDegraded) {
    return null;
  }

  const isAdmin = Boolean(user.is_admin);

  return (
    <div className="backend-status-banner" role="status" aria-live="polite">
      <div className="backend-status-card">
        <div className="backend-status-header">
          <span className="backend-status-icon" aria-hidden="true">
            ⚠
          </span>
          <span className="backend-status-title">
            {isAdmin ? "下载后端异常" : "服务异常"}
          </span>
          <button
            type="button"
            className="backend-status-close"
            aria-label="关闭服务状态提醒"
            onClick={() => setDismissedWhileDegraded(true)}
          >
            ✕
          </button>
        </div>
        <p className="backend-status-text">{backend.message}</p>
        {isAdmin ? (
          <div className="backend-status-footer">
            <button
              type="button"
              className="backend-status-action"
              onClick={() => push("/settings")}
            >
              查看系统设置
            </button>
          </div>
        ) : null}
      </div>

      <style>{`
        .backend-status-banner {
          position: fixed;
          top: 16px;
          left: 50%;
          transform: translateX(-50%);
          z-index: 1000;
          max-width: min(420px, calc(100vw - 32px));
          animation: backendStatusSlideIn 0.25s ease-out;
        }

        .backend-status-card {
          background: #fff8eb;
          border: 1px solid rgba(245, 158, 11, 0.35);
          border-radius: 14px;
          padding: 14px 16px;
          box-shadow: 0 8px 28px rgba(0, 0, 0, 0.08);
        }

        .backend-status-header {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 6px;
        }

        .backend-status-icon {
          color: #d97706;
          font-size: 14px;
        }

        .backend-status-title {
          flex: 1;
          font-size: 14px;
          font-weight: 600;
          color: #1a1a1a;
        }

        .backend-status-close {
          width: 22px;
          height: 22px;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.05);
          border: none;
          color: #888;
          cursor: pointer;
          font-size: 11px;
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .backend-status-close:hover {
          background: rgba(0, 0, 0, 0.1);
          color: #555;
        }

        .backend-status-text {
          margin: 0;
          font-size: 13px;
          color: #555;
          line-height: 1.5;
        }

        .backend-status-footer {
          margin-top: 10px;
        }

        .backend-status-action {
          width: 100%;
          padding: 8px 12px;
          background: #fff;
          color: #1a1a1a;
          border: 1px solid rgba(0, 0, 0, 0.1);
          border-radius: 8px;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
        }

        .backend-status-action:hover {
          background: #f7f7f7;
        }

        @keyframes backendStatusSlideIn {
          from {
            opacity: 0;
            transform: translate(-50%, -8px);
          }
          to {
            opacity: 1;
            transform: translate(-50%, 0);
          }
        }
      `}</style>
    </div>
  );
}
