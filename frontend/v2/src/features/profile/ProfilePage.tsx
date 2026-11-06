import { useCallback, useEffect, useState, type FormEvent } from "react";

import { useAuth, useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import { ConfirmDialog } from "@/shared/components/ui";
import { useLocalState } from "@/shared/hooks/useLocalState";
import {
  notificationPermission,
  requestNotificationPermission,
  type PermissionState,
} from "@/shared/notifications";
import type { RpcAccessResponse } from "@/shared/api/types";
import { changePassword, fetchRpcAccess, refreshRpcSecret, rpcEndpointUrl, setRpcAccess } from "./api";

const MIN_PASSWORD_LENGTH = 6;

export function ProfilePage() {
  const { user, refresh } = useAuth();
  const { push } = useToast();

  // —— 改密 ——
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [changing, setChanging] = useState(false);

  // —— 通知 ——
  const notifications = useLocalState("notifications.enabled", false);
  const onCompleted = useLocalState("notifications.on_completed", true);
  const onFailed = useLocalState("notifications.on_failed", true);
  const [permission, setPermission] = useState<PermissionState | "unsupported">(() =>
    notificationPermission(),
  );
  const notificationSupported = permission !== "unsupported";

  // —— RPC ——
  const [rpc, setRpc] = useState<RpcAccessResponse | null>(null);
  const [rpcError, setRpcError] = useState<string | null>(null);
  const [rpcBusy, setRpcBusy] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [confirmRefresh, setConfirmRefresh] = useState(false);

  useEffect(() => {
    setPermission(notificationPermission());
    let cancelled = false;
    // RPC 首读失败不阻塞改密区域，仅在 RPC 分区显示错误。
    fetchRpcAccess()
      .then((value) => {
        if (!cancelled) setRpc(value);
      })
      .catch((caught) => {
        if (!cancelled) setRpcError(errorMessage(caught, "RPC 状态加载失败"));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSubmitPassword = async (event: FormEvent) => {
    event.preventDefault();
    if (changing) return;
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      setPasswordError(`新密码长度至少 ${MIN_PASSWORD_LENGTH} 位`);
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("两次输入的新密码不一致");
      return;
    }
    setPasswordError(null);
    setChanging(true);
    try {
      await changePassword(
        user?.username ?? "",
        user?.is_initial_password === true ? null : oldPassword,
        newPassword,
      );
      push("success", "密码修改成功");
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
      await refresh(); // is_initial_password 变化会随之刷新横幅。
    } catch (caught) {
      setPasswordError(errorMessage(caught, "密码修改失败"));
    } finally {
      setChanging(false);
    }
  };

  const handleNotificationToggle = async (enabled: boolean) => {
    if (!enabled) {
      notifications.setValue(false);
      return;
    }
    if (!notificationSupported) return;
    const result = await requestNotificationPermission();
    setPermission(result);
    if (result !== "granted") {
      push("error", "浏览器通知权限被拒绝，请在浏览器设置中允许通知");
      return;
    }
    notifications.setValue(true);
  };

  const handleRpcToggle = async (enabled: boolean) => {
    setRpcBusy(true);
    setRpcError(null);
    try {
      setRpc(await setRpcAccess(enabled));
      push("success", enabled ? "RPC 访问已开启" : "RPC 访问已关闭");
    } catch (caught) {
      setRpcError(errorMessage(caught, "RPC 访问设置失败"));
    } finally {
      setRpcBusy(false);
    }
  };

  const handleRefreshSecret = async () => {
    setConfirmRefresh(false);
    setRpcBusy(true);
    setRpcError(null);
    try {
      const value = await refreshRpcSecret();
      setRpc(value);
      setRevealedSecret(value.secret ?? null);
      push("success", "RPC Secret 已刷新");
    } catch (caught) {
      setRpcError(errorMessage(caught, "RPC Secret 刷新失败"));
    } finally {
      setRpcBusy(false);
    }
  };

  const copyText = useCallback(
    async (text: string, label: string) => {
      try {
        await navigator.clipboard.writeText(text);
        push("success", `已复制${label}`);
      } catch {
        push("error", "复制失败，请手动复制");
      }
    },
    [push],
  );

  const isInitial = user?.is_initial_password === true;

  return (
    <section className="page" aria-label="个人资料">
      <header className="page-header">
        <h1>个人资料</h1>
        <span className="header-meta">{user?.username ?? ""}</span>
      </header>

      <form className="section-card" aria-label="修改密码" onSubmit={handleSubmitPassword}>
        <h2 className="section-title">修改密码</h2>
        {isInitial && (
          <div className="notice-banner" role="status">
            当前仍在使用初始密码：本次修改无需旧密码，请立即设置新密码。
          </div>
        )}
        {passwordError !== null && (
          <div className="form-error" role="alert">
            {passwordError}
          </div>
        )}
        <div className="form-grid">
          {!isInitial && (
            <label className="form-field">
              <span>旧密码</span>
              <input
                type="password"
                value={oldPassword}
                onChange={(event) => setOldPassword(event.target.value)}
                autoComplete="current-password"
              />
            </label>
          )}
          <label className="form-field">
            <span>新密码</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label className="form-field">
            <span>确认新密码</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>
        </div>
        <div className="section-actions">
          <button type="submit" className="primary-button" disabled={changing}>
            {changing ? "提交中…" : "修改密码"}
          </button>
        </div>
      </form>

      <div className="section-card" aria-label="桌面通知">
        <h2 className="section-title">桌面通知</h2>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={notifications.value}
            onChange={(event) => void handleNotificationToggle(event.target.checked)}
            aria-label="启用桌面通知"
            disabled={!notificationSupported}
          />
          <span>启用桌面通知（任务完成等事件提醒）</span>
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={onCompleted.value}
            onChange={(event) => onCompleted.setValue(event.target.checked)}
            aria-label="下载完成时通知"
            disabled={!notifications.value}
          />
          <span>下载完成时通知</span>
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={onFailed.value}
            onChange={(event) => onFailed.setValue(event.target.checked)}
            aria-label="下载失败时通知"
            disabled={!notifications.value}
          />
          <span>下载失败时通知</span>
        </label>
        <p className="hint-text">
          {!notificationSupported
            ? "当前浏览器不支持桌面通知。"
            : permission === "denied"
              ? "通知权限已被拒绝，请在浏览器设置中重新允许。"
              : "开启时将向浏览器申请通知权限。"}
        </p>
      </div>

      <div className="section-card" aria-label="RPC 访问">
        <h2 className="section-title">RPC 访问</h2>
        <p className="hint-text">
          供 AriaNg 等外部工具通过 JSON-RPC 接入，地址与当前站点同源。
        </p>
        <dl className="kv-list">
          <div className="kv-row">
            <dt>RPC 地址</dt>
            <dd>
              <code className="code-text">{rpcEndpointUrl()}</code>
              <button
                type="button"
                className="link-button"
                onClick={() => void copyText(rpcEndpointUrl(), "RPC 地址")}
              >
                复制
              </button>
            </dd>
          </div>
          <div className="kv-row">
            <dt>状态</dt>
            <dd>
              <label className="switch-row">
                <input
                  type="checkbox"
                  checked={rpc?.enabled ?? false}
                  onChange={(event) => void handleRpcToggle(event.target.checked)}
                  aria-label="开启 RPC 访问"
                  disabled={rpc === null || rpcBusy}
                />
                <span>{rpc === null ? "加载中…" : rpc.enabled ? "已开启" : "已关闭"}</span>
              </label>
            </dd>
          </div>
          {rpc?.secret_prefix != null && (
            <div className="kv-row">
              <dt>Secret 前缀</dt>
              <dd>
                <code className="code-text">{rpc.enabled ? rpc.secret_prefix : "••••••"}</code>
              </dd>
            </div>
          )}
        </dl>
        {revealedSecret !== null && (
          <div className="secret-reveal" role="status">
            <p>新 Secret（仅本次显示，请立即保存）：</p>
            <code className="code-text">{revealedSecret}</code>
            <button
              type="button"
              className="link-button"
              onClick={() => void copyText(revealedSecret, "Secret")}
            >
              复制
            </button>
          </div>
        )}
        <div className="section-actions">
          <button
            type="button"
            className="danger-button"
            disabled={rpc === null || !rpc.enabled || rpcBusy}
            onClick={() => setConfirmRefresh(true)}
          >
            刷新 Secret
          </button>
        </div>
        {rpcError !== null && (
          <div className="form-error" role="alert">
            {rpcError}
          </div>
        )}
      </div>

      {confirmRefresh && (
        <ConfirmDialog
          title="刷新 RPC Secret"
          message="刷新后旧的 Secret 将立即失效，使用旧 Secret 的外部工具会连接失败。确定继续？"
          confirmLabel="刷新"
          danger
          onConfirm={() => void handleRefreshSecret()}
          onCancel={() => setConfirmRefresh(false)}
        />
      )}
    </section>
  );
}
