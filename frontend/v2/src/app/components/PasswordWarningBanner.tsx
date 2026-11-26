"use client";

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { UserResponse } from "@/shared/api/types";
import { Icon } from "@/shared/components/Icon";
import { NotificationStackItem } from "@/shared/components/NotificationStack";

interface Props {
  user: UserResponse | null;
}

export function PasswordWarningBanner({ user }: Props) {
  const navigate = useNavigate();
  const [dismissed, setDismissed] = useState(false);

  if (!user?.is_initial_password || dismissed) {
    return null;
  }

  return (
    <NotificationStackItem order={0}>
      <div className="notification-card notification-card-panel password-warning-banner tone-alert">
        <div className="notification-card-header">
          <span className="notification-card-icon">
            <Icon name="alert" className="icon-sm" />
          </span>
          <span className="notification-card-title">安全提醒</span>
          <button
            type="button"
            className="notification-card-close"
            aria-label="关闭安全提醒"
            title="关闭"
            onClick={() => setDismissed(true)}
          >
            <Icon name="x" className="icon-sm" />
          </button>
        </div>
        <p className="notification-card-text">
          您<span className="text-danger">正在使用默认密码</span>，请尽快修改以确保账户安全
        </p>
        <div className="notification-card-footer">
          <button
            type="button"
            className="notification-card-action"
            onClick={() => navigate("/profile")}
          >
            前往修改
          </button>
        </div>
      </div>
    </NotificationStackItem>
  );
}
