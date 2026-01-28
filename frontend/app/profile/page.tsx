"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
  type NotificationSettings,
} from "@/lib/notification";
import { useToast } from "@/components/Toast";
import { useAuth } from "@/lib/AuthContext";
import { RpcAccessStatus } from "@/types";
import AuthLayout from "@/components/AuthLayout";

export default function ProfilePage() {
  const { showToast, showConfirm } = useToast();
  const { user, refreshUser } = useAuth();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 修改密码表单
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordChanging, setPasswordChanging] = useState(false);

  const [notificationSettings, setNotificationSettings] = useState<NotificationSettings>({
    enabled: false,
    onComplete: true,
    onError: true,
  });
  const [notificationSupported, setNotificationSupported] = useState(false);

  const [rpcAccess, setRpcAccess] = useState<RpcAccessStatus | null>(null);
  const [rpcLoading, setRpcLoading] = useState(false);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);

  useEffect(() => {
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
    const confirmed = await showConfirm({
      title: "刷新密钥",
      message: "刷新后旧的密钥将立即失效，确定继续？",
      confirmText: "刷新",
      danger: true,
    });
    if (!confirmed) return;
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

  async function handleChangePassword(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }

    if (newPassword.length < 6) {
      setError("新密码长度至少为 6 位");
      return;
    }

    setPasswordChanging(true);
    try {
      // 需要传入当前用户名
      await api.changePassword(oldPassword, newPassword, user!.username);
      showToast("密码修改成功", "success");
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
      await refreshUser();
    } catch (err) {
      const message = (err as Error).message;
      try {
        const parsed = JSON.parse(message);
        setError(parsed.detail || "密码修改失败");
      } catch {
        setError(message || "密码修改失败");
      }
    } finally {
      setPasswordChanging(false);
    }
  }

  async function handleNotificationToggle(enabled: boolean) {
    if (enabled) {
      const granted = await requestNotificationPermission();
      if (!granted) {
        showToast("浏览器通知权限被拒绝，请在浏览器设置中允许通知", "warning");
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
        <div className="page-header">
          <h1 className="page-title">用户设置</h1>
          <p className="muted">个人偏好与外部访问</p>
        </div>

        {error && (
          <div className="card text-danger mb-6">{error}</div>
        )}

        <div className="card mb-6">
          <h2 className="section-title">修改密码</h2>
          <form onSubmit={handleChangePassword} className="max-w-400">
            <div className="mb-4">
              <label className="form-label">当前密码</label>
              <input
                type="password"
                className="input"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                required
              />
            </div>
            <div className="mb-4">
              <label className="form-label">新密码</label>
              <input
                type="password"
                className="input"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                minLength={6}
                required
              />
              <p className="muted text-sm mt-1">至少 6 位字符</p>
            </div>
            <div className="mb-6">
              <label className="form-label">确认新密码</label>
              <input
                type="password"
                className="input"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                minLength={6}
                required
              />
            </div>
            <button
              type="submit"
              className="button"
              disabled={passwordChanging}
            >
              {passwordChanging ? "修改中..." : "修改密码"}
            </button>
          </form>
        </div>

        <div className="card mb-6">
          <h2 className="section-title">浏览器通知</h2>

          {!notificationSupported ? (
            <div className="alert alert-warning">
              <p>您的浏览器不支持通知功能</p>
            </div>
          ) : (
            <div className="max-w-600">
              <div className="mb-6">
                <div className="flex-between mb-2">
                  <label className="font-semibold">启用通知</label>
                  <button
                    type="button"
                    onClick={() => handleNotificationToggle(!notificationSettings.enabled)}
                    className={`toggle-switch ${notificationSettings.enabled ? "toggle-switch-on" : "toggle-switch-off"}`}
                  >
                    <div
                      className="toggle-knob"
                      style={{ left: notificationSettings.enabled ? 24 : 2 }}
                    />
                  </button>
                </div>
                <p className="muted text-sm">当下载任务状态变化时，发送浏览器桌面通知</p>
              </div>

              {notificationSettings.enabled && (
                <div className="bg-black-02 rounded-lg p-4">
                  <p className="muted text-sm mb-4">选择何时发送通知：</p>

                  <div className="mb-4">
                    <div className="flex-between">
                      <label className="text-base">下载完成时</label>
                      <button
                        type="button"
                        onClick={() => handleNotificationOptionChange("onComplete", !notificationSettings.onComplete)}
                        className={`toggle-switch toggle-switch-sm ${notificationSettings.onComplete ? "toggle-switch-on" : "toggle-switch-off"}`}
                      >
                        <div
                          className="toggle-knob toggle-knob-sm"
                          style={{ left: notificationSettings.onComplete ? 22 : 2 }}
                        />
                      </button>
                    </div>
                  </div>

                  <div>
                    <div className="flex-between">
                      <label className="text-base">下载失败时</label>
                      <button
                        type="button"
                        onClick={() => handleNotificationOptionChange("onError", !notificationSettings.onError)}
                        className={`toggle-switch toggle-switch-sm ${notificationSettings.onError ? "toggle-switch-on" : "toggle-switch-off"}`}
                      >
                        <div
                          className="toggle-knob toggle-knob-sm"
                          style={{ left: notificationSettings.onError ? 22 : 2 }}
                        />
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="card">
          <h2 className="section-title">外部访问</h2>

          <div className="max-w-600">
            <div className={`bg-black-02 rounded-lg p-4 ${rpcAccess?.enabled ? "mb-6" : ""}`}>
              <div className="flex-between mb-2">
                <label className="font-semibold">允许外部 aria2 客户端连接</label>
                <button
                  type="button"
                  onClick={() => handleRpcToggle(!rpcAccess?.enabled)}
                  disabled={rpcLoading}
                  className={`toggle-switch ${rpcAccess?.enabled ? "toggle-switch-on" : "toggle-switch-off"} ${rpcLoading ? "opacity-60 cursor-not-allowed" : ""}`}
                >
                  <div
                    className="toggle-knob"
                    style={{ left: rpcAccess?.enabled ? 24 : 2 }}
                  />
                </button>
              </div>
              <p className="muted text-sm">开启后可使用 AriaNg、Motrix 等客户端管理下载任务</p>
            </div>

            {rpcAccess?.enabled && rpcAccess.secret && (
              <div className="alert alert-info p-4">
                <div className="mb-5">
                  <div className="flex-between mb-2">
                    <label className="font-semibold text-base">RPC 密钥</label>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="button secondary btn-sm"
                        onClick={copySecret}
                      >
                        {copiedSecret ? "已复制" : "复制"}
                      </button>
                      <button
                        type="button"
                        className="button secondary btn-sm"
                        onClick={handleRefreshSecret}
                        disabled={rpcLoading}
                        style={{ opacity: rpcLoading ? 0.6 : 1 }}
                      >
                        刷新
                      </button>
                    </div>
                  </div>
                  <code className="code-block">{rpcAccess.secret}</code>
                </div>

                <div>
                  <div className="flex-between mb-2">
                    <label className="font-semibold text-base">RPC 地址</label>
                    <button
                      type="button"
                      className="button secondary btn-sm"
                      onClick={copyRpcUrl}
                    >
                      {copiedUrl ? "已复制" : "复制"}
                    </button>
                  </div>
                  <code className="code-block">{getRpcUrl()}</code>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </AuthLayout>
  );
}
