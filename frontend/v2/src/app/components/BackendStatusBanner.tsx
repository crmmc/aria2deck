"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/shared/api/client";
import { Icon } from "@/shared/components/Icon";
import { NotificationStackItem } from "@/shared/components/NotificationStack";
import type { BackendStatusResponse, UserResponse } from "@/shared/api/types";

const POLL_INTERVAL_MS = 20_000;

type BackendStatus = {
  status: "ok" | "degraded";
  message: string;
};

type Props = {
  user: UserResponse | null;
};

interface SystemStatusEnvelope {
  download_backend: BackendStatusResponse;
}

export function BackendStatusBanner({ user }: Props) {
  const navigate = useNavigate();
  const [backend, setBackend] = useState<BackendStatus | null>(null);
  const [dismissedWhileDegraded, setDismissedWhileDegraded] = useState(false);
  const mountedRef = useRef(true);
  const previousStatusRef = useRef<BackendStatus["status"] | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const result = await api.get<SystemStatusEnvelope>("/api/v2/system/status");
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
      if (document.hidden) return;
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
    <NotificationStackItem order={0}>
      <div
        className="notification-card notification-card-panel backend-status-banner tone-alert"
        role="status"
        aria-live="polite"
      >
        <div className="notification-card-header">
          <span className="notification-card-icon" aria-hidden="true">
            <Icon name="alert" className="icon-sm" />
          </span>
          <span className="notification-card-title">
            {isAdmin ? "下载后端异常" : "服务异常"}
          </span>
          <button
            type="button"
            className="notification-card-close"
            aria-label="关闭服务状态提醒"
            title="关闭"
            onClick={() => setDismissedWhileDegraded(true)}
          >
            <Icon name="x" className="icon-sm" />
          </button>
        </div>
        <p className="notification-card-text">{backend.message}</p>
        {isAdmin ? (
          <div className="notification-card-footer">
            <button
              type="button"
              className="notification-card-action"
              onClick={() => navigate("/settings")}
            >
              查看系统设置
            </button>
          </div>
        ) : null}
      </div>
    </NotificationStackItem>
  );
}
