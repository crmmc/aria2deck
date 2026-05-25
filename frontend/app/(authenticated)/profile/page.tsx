"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
  type NotificationSettings,
} from "@/lib/notification";
import { useMounted } from "@/lib/useMounted";
import { useToast } from "@/components/Toast";
import { useAuth } from "@/lib/AuthContext";
import { RpcAccessStatus } from "@/types";
import { InitialPasswordAlert } from "./_components/InitialPasswordAlert";
import { PasswordSection } from "./_components/PasswordSection";
import { NotificationSection } from "./_components/NotificationSection";
import { RpcAccessSection } from "./_components/RpcAccessSection";

export default function ProfilePage() {
  const { showToast, showConfirm } = useToast();
  const { user, refreshUser } = useAuth();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mounted = useMounted();

  const [showInitialPasswordAlert, setShowInitialPasswordAlert] = useState(false);

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
  const copyTimers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setNotificationSettings(getNotificationSettings());
    setNotificationSupported(typeof window !== "undefined" && "Notification" in window);
    loadRpcAccess().finally(() => {
      if (mountedRef.current) setLoading(false);
    });

    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      if (params.get("initial_password") === "1") {
        setShowInitialPasswordAlert(true);
      }
    }
    return () => {
      mountedRef.current = false;
      copyTimers.current.forEach(clearTimeout);
    };
  }, []);

  const loadRpcAccess = async () => {
    try {
      const data = await api.getRpcAccess();
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      console.error("加载 RPC 访问状态失败", err);
    }
  };

  const handleRpcToggle = async (enabled: boolean) => {
    setRpcLoading(true);
    try {
      const data = await api.setRpcAccess(enabled);
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      if (!mountedRef.current) return;
      console.error("设置 RPC 访问失败", err);
      setError("设置 RPC 访问失败: " + (err as Error).message);
    } finally {
      if (mountedRef.current) setRpcLoading(false);
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
    if (!mountedRef.current) return;
    setRpcLoading(true);
    try {
      const data = await api.refreshRpcSecret();
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      if (!mountedRef.current) return;
      console.error("刷新 Secret 失败", err);
      setError("刷新 Secret 失败: " + (err as Error).message);
    } finally {
      if (mountedRef.current) setRpcLoading(false);
    }
  };

  const copySecret = () => {
    if (rpcAccess?.secret) {
      navigator.clipboard.writeText(rpcAccess.secret).then(() => {
        if (!mountedRef.current) return;
        setCopiedSecret(true);
        const t = setTimeout(() => {
          if (!mountedRef.current) return;
          setCopiedSecret(false);
          copyTimers.current.delete(t);
        }, 2000);
        copyTimers.current.add(t);
      }).catch(() => {
        showToast("复制失败", "error");
      });
    }
  };

  const copyRpcUrl = () => {
    const url = getRpcUrl();
    if (url) {
      navigator.clipboard.writeText(url).then(() => {
        if (!mountedRef.current) return;
        setCopiedUrl(true);
        const t = setTimeout(() => {
          if (!mountedRef.current) return;
          setCopiedUrl(false);
          copyTimers.current.delete(t);
        }, 2000);
        copyTimers.current.add(t);
      }).catch(() => {
        showToast("复制失败", "error");
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
    if (!user?.username) {
      setError("用户信息未加载，请刷新页面后重试");
      return;
    }

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
      await api.changePassword(oldPassword, newPassword, user.username);
      if (mountedRef.current) {
        showToast("密码修改成功", "success");
        setOldPassword("");
        setNewPassword("");
        setConfirmPassword("");
        await refreshUser();
      }
    } catch (err) {
      if (!mountedRef.current) return;
      const message = (err as Error).message;
      try {
        const parsed = JSON.parse(message);
        setError(parsed.detail || "密码修改失败");
      } catch {
        setError(message || "密码修改失败");
      }
    } finally {
      if (mountedRef.current) setPasswordChanging(false);
    }
  }

  async function handleNotificationToggle(enabled: boolean) {
    if (enabled) {
      const granted = await requestNotificationPermission();
      if (mountedRef.current && !granted) {
        showToast("浏览器通知权限被拒绝，请在浏览器设置中允许通知", "warning");
      }
      if (!granted) return;
    }
    if (mountedRef.current) {
      const newSettings = { ...notificationSettings, enabled };
      setNotificationSettings(newSettings);
      saveNotificationSettings(newSettings);
    }
  }

  function handleNotificationOptionChange(key: "onComplete" | "onError", value: boolean) {
    if (!mountedRef.current) return;
    const newSettings = { ...notificationSettings, [key]: value };
    setNotificationSettings(newSettings);
    saveNotificationSettings(newSettings);
  }

  if (loading) return null;

  return (
    <>
      {mounted && (
        <InitialPasswordAlert
          open={showInitialPasswordAlert}
          onClose={() => setShowInitialPasswordAlert(false)}
        />
      )}

      <div className="glass-frame full-height animate-in">
        <div className="page-header">
          <h1 className="page-title">用户设置</h1>
          <p className="muted">个人偏好与外部访问</p>
        </div>

        {error && (
          <div className="card text-danger mb-6">{error}</div>
        )}

        <PasswordSection
          isInitialPassword={!!user?.is_initial_password}
          oldPassword={oldPassword}
          newPassword={newPassword}
          confirmPassword={confirmPassword}
          changing={passwordChanging}
          onOldPasswordChange={setOldPassword}
          onNewPasswordChange={setNewPassword}
          onConfirmPasswordChange={setConfirmPassword}
          onSubmit={handleChangePassword}
        />

        <NotificationSection
          supported={notificationSupported}
          settings={notificationSettings}
          onEnabledChange={handleNotificationToggle}
          onOptionChange={handleNotificationOptionChange}
        />

        <RpcAccessSection
          rpcAccess={rpcAccess}
          rpcLoading={rpcLoading}
          copiedSecret={copiedSecret}
          copiedUrl={copiedUrl}
          rpcUrl={getRpcUrl()}
          onToggle={handleRpcToggle}
          onRefreshSecret={handleRefreshSecret}
          onCopySecret={copySecret}
          onCopyRpcUrl={copyRpcUrl}
        />
      </div>
    </>
  );
}
