"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { User } from "@/types";

interface Props {
  user: User | null;
}

export default function PasswordWarningBanner({ user }: Props) {
  const router = useRouter();
  const [dismissed, setDismissed] = useState(false);

  if (!user?.is_default_password || dismissed) {
    return null;
  }

  return (
    <div className="password-warning-banner">
      <div className="password-warning-card">
        <div className="password-warning-header">
          <span className="password-warning-icon">⚠️</span>
          <span className="password-warning-title">安全提醒</span>
          <button
            className="password-warning-close"
            onClick={() => setDismissed(true)}
            title="关闭"
          >
            ✕
          </button>
        </div>
        <p className="password-warning-text">
          您<span className="text-danger">正在使用默认密码</span>，请尽快修改以确保账户安全
        </p>
        <div className="password-warning-footer">
          <button
            className="password-warning-action"
            onClick={() => router.push("/profile")}
          >
            前往修改
          </button>
        </div>
      </div>

      <style>{`
        .password-warning-banner {
          position: fixed;
          top: 16px;
          right: 16px;
          z-index: 1000;
          animation: slideIn 0.3s ease-out;
        }

        .password-warning-card {
          width: 280px;
          background: rgba(255, 255, 255, 0.9);
          border: 1px solid rgba(255, 255, 255, 0.6);
          border-radius: 16px;
          padding: 16px;
          box-shadow:
            0 8px 32px rgba(0, 0, 0, 0.08),
            0 1px 2px rgba(255, 255, 255, 0.5) inset;
        }

        .password-warning-header {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 8px;
        }

        .password-warning-icon {
          font-size: 16px;
        }

        .password-warning-title {
          flex: 1;
          font-size: 14px;
          font-weight: 600;
          color: #1a1a1a;
        }

        .password-warning-close {
          width: 20px;
          height: 20px;
          border-radius: 50%;
          background: rgba(0, 0, 0, 0.05);
          border: none;
          color: #999;
          cursor: pointer;
          font-size: 10px;
          display: flex;
          align-items: center;
          justify-content: center;
          transition: all 0.15s ease;
        }

        .password-warning-close:hover {
          background: rgba(0, 0, 0, 0.1);
          color: #666;
        }

        .password-warning-text {
          margin: 0 0 12px 0;
          font-size: 13px;
          color: #666;
          line-height: 1.5;
        }

        .password-warning-text .text-danger {
          color: #ef4444;
          font-weight: 500;
        }

        .password-warning-footer {
          display: flex;
        }

        .password-warning-action {
          width: 100%;
          padding: 8px 16px;
          background: #fff;
          color: #1a1a1a;
          border: 1px solid rgba(0, 0, 0, 0.1);
          border-radius: 8px;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.15s ease;
        }

        .password-warning-action:hover {
          background: #f5f5f5;
        }

        @keyframes slideIn {
          from {
            opacity: 0;
            transform: translateY(-10px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
      `}</style>
    </div>
  );
}
