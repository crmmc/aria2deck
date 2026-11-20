"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import type { RpcAccessResponse } from "@/shared/api/types";
import { useToast } from "@/shared/components/toast-context";
import { useClipboard } from "@/shared/hooks/useClipboard";
import { useLocalState } from "@/shared/hooks/useLocalState";
import { useMounted } from "@/shared/hooks/useMounted";
import { requestNotificationPermission } from "@/shared/notifications";
import {
  changePassword,
  fetchRpcAccess,
  refreshRpcSecret,
  rpcEndpointUrl,
  updateRpcAccess,
} from "./api";
import { InitialPasswordAlert } from "./components/InitialPasswordAlert";
import {
  NotificationSection,
  type NotificationSettings,
} from "./components/NotificationSection";
import { PasswordSection } from "./components/PasswordSection";
import { RpcAccessSection } from "./components/RpcAccessSection";

export function ProfilePage() {
  const { showToast, showConfirm } = useToast();
  const { user, refresh } = useAuth();
  const copyToClipboard = useClipboard();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mounted = useMounted();

  const [showInitialPasswordAlert, setShowInitialPasswordAlert] = useState(false);

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordChanging, setPasswordChanging] = useState(false);

  // 通知读写经 useLocalState 白名单键（C33：不做 归档参考 旧键迁移，老用户回默认值）。
  const notifyEnabled = useLocalState("notifications.enabled", false);
  const notifyOnComplete = useLocalState("notifications.on_completed", true);
  const notifyOnError = useLocalState("notifications.on_failed", true);
  const [notificationSupported, setNotificationSupported] = useState(false);

  const [rpcAccess, setRpcAccess] = useState<RpcAccessResponse | null>(null);
  const [rpcLoading, setRpcLoading] = useState(false);
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const copyTimers = useRef<Set<ReturnType<typeof setTimeout>> | null>(null);
  if (copyTimers.current === null) {
    copyTimers.current = new Set();
  }
  const copyTimerSet = copyTimers.current;
  const mountedRef = useRef(true);

  const loadRpcAccess = async () => {
    try {
      const data = await fetchRpcAccess();
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      console.error("加载 RPC 访问状态失败", err);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    setNotificationSupported(typeof window !== "undefined" && "Notification" in window);
    void loadRpcAccess().finally(() => {
      if (mountedRef.current) setLoading(false);
    });
    return () => {
      mountedRef.current = false;
      copyTimerSet.forEach(clearTimeout);
    };
  }, [copyTimerSet]);

  useEffect(() => {
    if (searchParams.get("initial_password") === "1") {
      setShowInitialPasswordAlert(true);
    }
  }, [searchParams]);

  const handleRpcToggle = async (enabled: boolean) => {
    setRpcLoading(true);
    try {
      const data = await updateRpcAccess(enabled);
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      if (!mountedRef.current) return;
      console.error("设置 RPC 访问失败", err);
      setError("设置 RPC 访问失败: " + errorMessage(err, "未知错误"));
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
      const data = await refreshRpcSecret();
      if (mountedRef.current) setRpcAccess(data);
    } catch (err) {
      if (!mountedRef.current) return;
      console.error("刷新 Secret 失败", err);
      setError("刷新 Secret 失败: " + errorMessage(err, "未知错误"));
    } finally {
      if (mountedRef.current) setRpcLoading(false);
    }
  };

  const copySecret = () => {
    if (rpcAccess?.secret) {
      void copyToClipboard(rpcAccess.secret, {
        successMessage: "已复制",
        onSuccess: () => {
          if (!mountedRef.current) return;
          setCopiedSecret(true);
          const t = setTimeout(() => {
            if (!mountedRef.current) return;
            setCopiedSecret(false);
            copyTimerSet.delete(t);
          }, 2000);
          copyTimerSet.add(t);
        },
      });
    }
  };

  const copyRpcUrl = () => {
    const url = rpcEndpointUrl();
    if (url) {
      void copyToClipboard(url, {
        successMessage: "已复制",
        onSuccess: () => {
          if (!mountedRef.current) return;
          setCopiedUrl(true);
          const t = setTimeout(() => {
            if (!mountedRef.current) return;
            setCopiedUrl(false);
            copyTimerSet.delete(t);
          }, 2000);
          copyTimerSet.add(t);
        },
      });
    }
  };

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
      await changePassword(
        user.username,
        user.is_initial_password === true ? null : oldPassword,
        newPassword,
      );
      if (mountedRef.current) {
        showToast("密码修改成功", "success");
        setOldPassword("");
        setNewPassword("");
        setConfirmPassword("");
        await refresh();
      }
    } catch (err) {
      if (!mountedRef.current) return;
      setError(errorMessage(err, "密码修改失败"));
    } finally {
      if (mountedRef.current) setPasswordChanging(false);
    }
  }

  async function handleNotificationToggle(enabled: boolean) {
    if (enabled) {
      const granted = (await requestNotificationPermission()) === "granted";
      if (mountedRef.current && !granted) {
        showToast("浏览器通知权限被拒绝，请在浏览器设置中允许通知", "warning");
      }
      if (!granted) return;
    }
    if (mountedRef.current) {
      notifyEnabled.setValue(enabled);
    }
  }

  function handleNotificationOptionChange(key: "onComplete" | "onError", value: boolean) {
    if (!mountedRef.current) return;
    if (key === "onComplete") {
      notifyOnComplete.setValue(value);
      return;
    }
    notifyOnError.setValue(value);
  }

  if (loading) return null;

  const notificationSettings: NotificationSettings = {
    enabled: notifyEnabled.value,
    onComplete: notifyOnComplete.value,
    onError: notifyOnError.value,
  };

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
          isInitialPassword={user?.is_initial_password === true}
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
          rpcUrl={rpcEndpointUrl()}
          onToggle={handleRpcToggle}
          onRefreshSecret={handleRefreshSecret}
          onCopySecret={copySecret}
          onCopyRpcUrl={copyRpcUrl}
        />
      </div>
    </>
  );
}
