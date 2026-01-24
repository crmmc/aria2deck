"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
  type NotificationSettings,
} from "@/lib/notification";
import { RpcAccessStatus } from "@/types";
import AuthLayout from "@/components/AuthLayout";

export default function ProfilePage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 通知设置
  const [notificationSettings, setNotificationSettings] = useState<NotificationSettings>({
    enabled: false,
    onComplete: true,
    onError: true,
  });
  const [notificationSupported, setNotificationSupported] = useState(false);

  // RPC 访问管理状态
  const [rpcAccess, setRpcAccess] = useState<RpcAccessStatus | null>(null);
  const [rpcLoading, setRpcLoading] = useState(false);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);

  useEffect(() => {
    // 初始化通知设置
    setNotificationSettings(getNotificationSettings());
    setNotificationSupported(typeof window !== "undefined" && "Notification" in window);

    loadRpcAccess().then(() => setLoading(false));
  }, []);

  const loadRpcAccess = async () => {
    try {
      const data = await api.getRpcAccess();
      setRpcAccess(data);
    } catch (err) {
      console.error("加载 RPC 访问状态失败", err);
    }
  };

  const handleRpcToggle = async (enabled: boolean) => {
    setRpcLoading(true);
    try {
      const data = await api.setRpcAccess(enabled);
      setRpcAccess(data);
    } catch (err) {
      console.error("设置 RPC 访问失败", err);
      setError("设置 RPC 访问失败: " + (err as Error).message);
    } finally {
      setRpcLoading(false);
    }
  };

  const handleRefreshSecret = async () => {
    if (!confirm("刷新后旧的 Secret 将立即失效，确定继续？")) return;
    setRpcLoading(true);
    try {
      const data = await api.refreshRpcSecret();
      setRpcAccess(data);
    } catch (err) {
      console.error("刷新 Secret 失败", err);
      setError("刷新 Secret 失败: " + (err as Error).message);
    } finally {
      setRpcLoading(false);
    }
  };

  const copySecret = () => {
    if (rpcAccess?.secret) {
      navigator.clipboard.writeText(rpcAccess.secret).then(() => {
        setCopiedSecret(true);
        setTimeout(() => setCopiedSecret(false), 2000);
      });
    }
  };

  const copyRpcUrl = () => {
    const url = getRpcUrl();
    if (url) {
      navigator.clipboard.writeText(url).then(() => {
        setCopiedUrl(true);
        setTimeout(() => setCopiedUrl(false), 2000);
      });
    }
  };

  function getRpcUrl(): string {
    if (typeof window === "undefined") return "";
    return `${window.location.origin}/aria2/jsonrpc`;
  }

  function formatSecret(secret: string | null): string {
    if (!secret) return "";
    if (secret.length <= 16) return secret;
    return secret.slice(0, 12) + "..." + secret.slice(-4);
  }

  async function handleNotificationToggle(enabled: boolean) {
    if (enabled) {
      const granted = await requestNotificationPermission();
      if (!granted) {
        alert("浏览器通知权限被拒绝，请在浏览器设置中允许通知");
        return;
      }
    }
    const newSettings = { ...notificationSettings, enabled };
    setNotificationSettings(newSettings);
    saveNotificationSettings(newSettings);
  }

  function handleNotificationOptionChange(key: "onComplete" | "onError", value: boolean) {
    const newSettings = { ...notificationSettings, [key]: value };
    setNotificationSettings(newSettings);
    saveNotificationSettings(newSettings);
  }

  if (loading) return null;

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <h1 style={{ marginBottom: 8 }}>用户设置</h1>
        <p className="muted" style={{ marginBottom: 32 }}>
          个人偏好与外部访问
        </p>

        {error && (
          <div className="card" style={{ color: "var(--danger)", marginBottom: 24 }}>
            {error}
          </div>
        )}

        {/* 浏览器通知设置 */}
        <div className="card" style={{ marginBottom: 24 }}>
          <h2 style={{ marginBottom: 24 }}>浏览器通知</h2>

          {!notificationSupported ? (
            <div
              style={{
                padding: 16,
                background: "rgba(255, 149, 0, 0.1)",
                border: "1px solid rgba(255, 149, 0, 0.3)",
                borderRadius: 8,
              }}
            >
              <p style={{ margin: 0, color: "#ff9500" }}>
                您的浏览器不支持通知功能
              </p>
            </div>
          ) : (
            <div style={{ maxWidth: 600 }}>
              <div style={{ marginBottom: 24 }}>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 8,
                  }}
                >
                  <label style={{ fontWeight: 600 }}>启用通知</label>
                  <button
                    type="button"
                    onClick={() => handleNotificationToggle(!notificationSettings.enabled)}
                    style={{
                      width: 50,
                      height: 28,
                      borderRadius: 14,
                      border: "none",
                      cursor: "pointer",
                      background: notificationSettings.enabled ? "#34c759" : "rgba(0,0,0,0.1)",
                      position: "relative",
                      transition: "background 0.2s ease",
                    }}
                  >
                    <div
                      style={{
                        width: 24,
                        height: 24,
                        borderRadius: 12,
                        background: "white",
                        position: "absolute",
                        top: 2,
                        left: notificationSettings.enabled ? 24 : 2,
                        transition: "left 0.2s ease",
                        boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
                      }}
                    />
                  </button>
                </div>
                <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                  当下载任务状态变化时，发送浏览器桌面通知
                </p>
              </div>

              {notificationSettings.enabled && (
                <div
                  style={{
                    padding: 16,
                    background: "rgba(0,0,0,0.02)",
                    borderRadius: 8,
                  }}
                >
                  <p className="muted" style={{ fontSize: 13, marginBottom: 16 }}>
                    选择何时发送通知：
                  </p>

                  <div style={{ marginBottom: 16 }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                      }}
                    >
                      <label style={{ fontSize: 14 }}>下载完成时</label>
                      <button
                        type="button"
                        onClick={() => handleNotificationOptionChange("onComplete", !notificationSettings.onComplete)}
                        style={{
                          width: 44,
                          height: 24,
                          borderRadius: 12,
                          border: "none",
                          cursor: "pointer",
                          background: notificationSettings.onComplete ? "#34c759" : "rgba(0,0,0,0.1)",
                          position: "relative",
                          transition: "background 0.2s ease",
                        }}
                      >
                        <div
                          style={{
                            width: 20,
                            height: 20,
                            borderRadius: 10,
                            background: "white",
                            position: "absolute",
                            top: 2,
                            left: notificationSettings.onComplete ? 22 : 2,
                            transition: "left 0.2s ease",
                            boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
                          }}
                        />
                      </button>
                    </div>
                  </div>

                  <div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                      }}
                    >
                      <label style={{ fontSize: 14 }}>下载失败时</label>
                      <button
                        type="button"
                        onClick={() => handleNotificationOptionChange("onError", !notificationSettings.onError)}
                        style={{
                          width: 44,
                          height: 24,
                          borderRadius: 12,
                          border: "none",
                          cursor: "pointer",
                          background: notificationSettings.onError ? "#34c759" : "rgba(0,0,0,0.1)",
                          position: "relative",
                          transition: "background 0.2s ease",
                        }}
                      >
                        <div
                          style={{
                            width: 20,
                            height: 20,
                            borderRadius: 10,
                            background: "white",
                            position: "absolute",
                            top: 2,
                            left: notificationSettings.onError ? 22 : 2,
                            transition: "left 0.2s ease",
                            boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
                          }}
                        />
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* 外部访问管理 */}
        <div className="card">
          <h2 style={{ marginBottom: 24 }}>外部访问</h2>

          <div style={{ maxWidth: 600 }}>
            {/* 开关区域 */}
            <div
              style={{
                padding: 16,
                background: "rgba(0,0,0,0.02)",
                borderRadius: 8,
                marginBottom: rpcAccess?.enabled ? 24 : 0,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: 8,
                }}
              >
                <label style={{ fontWeight: 600 }}>允许外部 aria2 客户端连接</label>
                <button
                  type="button"
                  onClick={() => handleRpcToggle(!rpcAccess?.enabled)}
                  disabled={rpcLoading}
                  style={{
                    width: 50,
                    height: 28,
                    borderRadius: 14,
                    border: "none",
                    cursor: rpcLoading ? "not-allowed" : "pointer",
                    background: rpcAccess?.enabled ? "#34c759" : "rgba(0,0,0,0.1)",
                    position: "relative",
                    transition: "background 0.2s ease",
                    opacity: rpcLoading ? 0.6 : 1,
                  }}
                >
                  <div
                    style={{
                      width: 24,
                      height: 24,
                      borderRadius: 12,
                      background: "white",
                      position: "absolute",
                      top: 2,
                      left: rpcAccess?.enabled ? 24 : 2,
                      transition: "left 0.2s ease",
                      boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
                    }}
                  />
                </button>
              </div>
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                开启后可使用 AriaNg、Motrix 等客户端管理下载任务
              </p>
            </div>

            {/* RPC 配置详情（仅在开启后显示） */}
            {rpcAccess?.enabled && rpcAccess.secret && (
              <div
                style={{
                  padding: 16,
                  background: "rgba(0,113,227,0.05)",
                  border: "1px solid rgba(0,113,227,0.2)",
                  borderRadius: 8,
                }}
              >
                {/* RPC Secret */}
                <div style={{ marginBottom: 20 }}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      marginBottom: 8,
                    }}
                  >
                    <label style={{ fontWeight: 600, fontSize: 14 }}>RPC Secret</label>
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        type="button"
                        className="button secondary"
                        onClick={copySecret}
                        style={{ padding: "4px 12px", fontSize: 12 }}
                      >
                        {copiedSecret ? "已复制" : "复制"}
                      </button>
                      <button
                        type="button"
                        className="button secondary"
                        onClick={handleRefreshSecret}
                        disabled={rpcLoading}
                        style={{
                          padding: "4px 12px",
                          fontSize: 12,
                          opacity: rpcLoading ? 0.6 : 1,
                        }}
                      >
                        刷新
                      </button>
                    </div>
                  </div>
                  <code
                    style={{
                      display: "block",
                      padding: "10px 12px",
                      background: "rgba(0,0,0,0.05)",
                      borderRadius: 6,
                      fontFamily: "monospace",
                      fontSize: 13,
                      wordBreak: "break-all",
                    }}
                  >
                    {rpcAccess.secret}
                  </code>
                </div>

                {/* 连接配置 */}
                <div>
                  <p style={{ fontWeight: 600, fontSize: 14, marginBottom: 12 }}>
                    连接配置
                  </p>
                  <div style={{ fontSize: 13 }}>
                    <div style={{ marginBottom: 12 }}>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "space-between",
                          marginBottom: 4,
                        }}
                      >
                        <span className="muted">RPC 地址：</span>
                        <button
                          type="button"
                          className="button secondary"
                          onClick={copyRpcUrl}
                          style={{ padding: "2px 8px", fontSize: 11 }}
                        >
                          {copiedUrl ? "已复制" : "复制"}
                        </button>
                      </div>
                      <code
                        style={{
                          display: "block",
                          padding: "8px 10px",
                          background: "rgba(0,0,0,0.05)",
                          borderRadius: 4,
                          fontFamily: "monospace",
                          fontSize: 12,
                          wordBreak: "break-all",
                        }}
                      >
                        {getRpcUrl()}
                      </code>
                    </div>
                    <div>
                      <span className="muted">RPC 密钥：</span>
                      <code
                        style={{
                          padding: "2px 6px",
                          background: "rgba(0,0,0,0.05)",
                          borderRadius: 4,
                          fontFamily: "monospace",
                          fontSize: 12,
                        }}
                      >
                        token:{formatSecret(rpcAccess.secret)}
                      </code>
                    </div>
                  </div>
                  <p className="muted" style={{ fontSize: 12, marginTop: 12, marginBottom: 0 }}>
                    在客户端中填入上述 RPC 地址，密钥格式为 token:您的Secret
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </AuthLayout>
  );
}
