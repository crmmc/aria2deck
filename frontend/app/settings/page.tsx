"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { SystemConfig, MachineStats } from "@/types";
import AuthLayout from "@/components/AuthLayout";
import { formatBytes, bytesToGB, gbToBytes } from "@/lib/utils";

export default function SettingsPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [machineStats, setMachineStats] = useState<MachineStats | null>(null);

  const [maxTaskSize, setMaxTaskSize] = useState("");
  const [minFreeDisk, setMinFreeDisk] = useState("");
  const [aria2RpcUrl, setAria2RpcUrl] = useState("");
  const [aria2RpcSecret, setAria2RpcSecret] = useState("");
  const [hiddenExtensions, setHiddenExtensions] = useState<string[]>([]);
  const [extensionInput, setExtensionInput] = useState("");
  const [aria2Status, setAria2Status] = useState<{
    connected: boolean;
    version?: string;
    error?: string;
  } | null>(null);
  const [testResult, setTestResult] = useState<{
    connected: boolean;
    version?: string;
    error?: string;
  } | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);

  useEffect(() => {
    api
      .me()
      .then((user) => {
        if (!user.is_admin) {
          router.push("/tasks");
          return null;
        }
        return loadConfig();
      })
      .then(() => {
        setLoading(false);
      })
      .catch((err) => {
        setError("加载配置失败");
        setLoading(false);
      });
  }, [router]);

  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);

  async function loadConfig() {
    try {
      const [cfg, stats, aria2Ver] = await Promise.all([
        api.getConfig(),
        api.getMachineStats(),
        api.getAria2Version(),
      ]);
      setMaxTaskSize(bytesToGB(cfg.max_task_size));
      setMinFreeDisk(bytesToGB(cfg.min_free_disk));
      setAria2RpcUrl(cfg.aria2_rpc_url || "");
      setAria2RpcSecret(cfg.aria2_rpc_secret || "");
      setHiddenExtensions(cfg.hidden_file_extensions || []);
      setMachineStats(stats);
      setAria2Status(aria2Ver);
      setTestResult(null); // 清空测试结果
    } catch (err) {
      setError("加载配置失败");
    }
  }

  async function saveConfig(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveSuccess(false);
    try {
      const newMax = gbToBytes(parseFloat(maxTaskSize));
      const newMin = gbToBytes(parseFloat(minFreeDisk));

      await api.updateConfig({
        max_task_size: newMax,
        min_free_disk: newMin,
        aria2_rpc_url: aria2RpcUrl,
        aria2_rpc_secret: aria2RpcSecret.startsWith("*")
          ? undefined
          : aria2RpcSecret,
        hidden_file_extensions: hiddenExtensions,
      });

      // 保存后重新加载配置
      await loadConfig();
      setSaveSuccess(true);
      // 3秒后隐藏成功提示
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      setError("保存配置失败");
    } finally {
      setSaving(false);
    }
  }

  async function testConnection() {
    if (!aria2RpcUrl) {
      setTestResult({ connected: false, error: "请输入 aria2 RPC URL" });
      return;
    }

    setTestingConnection(true);
    setTestResult(null); // 清空之前的测试结果
    try {
      const result = await api.testAria2Connection(
        aria2RpcUrl,
        aria2RpcSecret.startsWith("*") ? undefined : aria2RpcSecret,
      );
      setTestResult(result);
    } catch (err) {
      setTestResult({ connected: false, error: (err as Error).message });
    } finally {
      setTestingConnection(false);
    }
  }

  function addExtension() {
    const ext = extensionInput.trim().toLowerCase();
    if (!ext) return;

    // 规范化：移除开头的点（后端会自动添加）
    const normalized = ext.startsWith(".") ? ext.substring(1) : ext;

    // 检查是否已存在
    const withDot = "." + normalized;
    if (hiddenExtensions.includes(withDot)) {
      setExtensionInput("");
      return;
    }

    setHiddenExtensions([...hiddenExtensions, withDot]);
    setExtensionInput("");
  }

  function removeExtension(ext: string) {
    setHiddenExtensions(hiddenExtensions.filter((e) => e !== ext));
  }

  function handleExtensionKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      addExtension();
    }
  }

  function addCommonExtension(ext: string) {
    const withDot = ext.startsWith(".") ? ext : "." + ext;
    if (!hiddenExtensions.includes(withDot)) {
      setHiddenExtensions([...hiddenExtensions, withDot]);
    }
  }

  if (loading) return null;

  return (
    <AuthLayout>
      <div className="glass-frame full-height animate-in">
        <h1 style={{ marginBottom: 8 }}>设置</h1>
        <p className="muted" style={{ marginBottom: 32 }}>
          系统配置
        </p>

        {error && (
          <div className="card" style={{ color: "var(--danger)" }}>
            {error}
          </div>
        )}

        {machineStats && (
          <div className="card" style={{ marginBottom: 24 }}>
            <h2 style={{ marginBottom: 16 }}>机器磁盘空间</h2>
            <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
              <span style={{ fontSize: 24, fontWeight: 600 }}>
                {formatBytes(machineStats.disk_free)}
              </span>
              <span className="muted" style={{ fontSize: 16 }}>
                / {formatBytes(machineStats.disk_total)}
              </span>
              <span className="muted">可用</span>
            </div>
            <div
              style={{
                height: 6,
                background: "rgba(0,0,0,0.05)",
                borderRadius: 3,
                marginTop: 8,
                overflow: "hidden",
                maxWidth: 600,
              }}
            >
              <div
                style={{
                  height: "100%",
                  width: `${(machineStats.disk_used / machineStats.disk_total) * 100}%`,
                  background:
                    (machineStats.disk_used / machineStats.disk_total) * 100 >=
                    80
                      ? "#ff3b30"
                      : (machineStats.disk_used / machineStats.disk_total) *
                            100 >=
                          50
                        ? "#ff9500"
                        : "#34c759",
                  transition: "width 0.5s ease, background 0.3s ease",
                }}
              />
            </div>
          </div>
        )}

        <div className="card">
          <form onSubmit={saveConfig} style={{ maxWidth: 600 }}>
            <h2 style={{ marginBottom: 24 }}>系统配置</h2>

            <div style={{ marginBottom: 24 }}>
              <label
                style={{ display: "block", marginBottom: 8, fontWeight: 600 }}
              >
                最大任务大小 (GB)
              </label>
              <p className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                超过此大小的任务将被拒绝。
              </p>
              <input
                className="input"
                type="number"
                step="0.1"
                min="0"
                max="10240"
                value={maxTaskSize}
                onChange={(e) => setMaxTaskSize(e.target.value)}
              />
            </div>

            <div style={{ marginBottom: 32 }}>
              <label
                style={{ display: "block", marginBottom: 8, fontWeight: 600 }}
              >
                最小剩余磁盘空间 (GB)
              </label>
              <p className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                如果剩余空间低于此值，将停止接受新任务。
              </p>
              <input
                className="input"
                type="number"
                step="0.1"
                min="0"
                max="10240"
                value={minFreeDisk}
                onChange={(e) => setMinFreeDisk(e.target.value)}
              />
            </div>

            <h2 style={{ marginBottom: 24 }}>aria2 后端配置</h2>

            {/* aria2 连接状态 */}
            <div
              style={{
                marginBottom: 24,
                padding: 16,
                background: aria2Status?.connected
                  ? "rgba(52, 199, 89, 0.1)"
                  : "rgba(255, 59, 48, 0.1)",
                border: `1px solid ${aria2Status?.connected ? "rgba(52, 199, 89, 0.3)" : "rgba(255, 59, 48, 0.3)"}`,
                borderRadius: 8,
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
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: aria2Status?.connected
                        ? "#34c759"
                        : "#ff3b30",
                    }}
                  />
                  <span style={{ fontWeight: 600 }}>
                    {aria2Status?.connected ? "已连接" : "未连接"}
                  </span>
                </div>
                {aria2Status?.connected && aria2Status.version && (
                  <span
                    className="muted"
                    style={{ fontSize: 13, fontFamily: "monospace" }}
                  >
                    aria2 {aria2Status.version}
                  </span>
                )}
              </div>
              {aria2Status?.error && (
                <p
                  className="muted"
                  style={{ fontSize: 13, margin: 0, color: "#ff3b30" }}
                >
                  错误：{aria2Status.error}
                </p>
              )}
            </div>

            <div style={{ marginBottom: 24 }}>
              <label
                style={{ display: "block", marginBottom: 8, fontWeight: 600 }}
              >
                aria2 RPC URL
              </label>
              <p className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                aria2 JSON-RPC 接口地址，例如：http://localhost:6800/jsonrpc
              </p>
              <input
                className="input"
                type="text"
                value={aria2RpcUrl}
                onChange={(e) => setAria2RpcUrl(e.target.value)}
                placeholder="http://localhost:6800/jsonrpc"
              />
            </div>

            <div style={{ marginBottom: 16 }}>
              <label
                style={{ display: "block", marginBottom: 8, fontWeight: 600 }}
              >
                aria2 RPC Secret
              </label>
              <p className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                aria2 RPC 认证密钥（可选）。留空表示不使用认证。
              </p>
              <input
                className="input"
                type="password"
                value={aria2RpcSecret}
                onChange={(e) => setAria2RpcSecret(e.target.value)}
                placeholder="留空表示无密钥"
              />
            </div>

            <div style={{ marginBottom: 32 }}>
              <button
                type="button"
                className="button secondary"
                onClick={testConnection}
                disabled={testingConnection}
                style={{ padding: "8px 16px", marginBottom: 16 }}
              >
                {testingConnection ? "测试中..." : "测试连接"}
              </button>

              {/* 测试结果显示 */}
              {testResult && (
                <div
                  style={{
                    padding: 12,
                    background: testResult.connected
                      ? "rgba(52, 199, 89, 0.1)"
                      : "rgba(255, 59, 48, 0.1)",
                    border: `1px solid ${testResult.connected ? "rgba(52, 199, 89, 0.3)" : "rgba(255, 59, 48, 0.3)"}`,
                    borderRadius: 8,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        background: testResult.connected ? "#34c759" : "#ff3b30",
                      }}
                    />
                    <span style={{ fontSize: 14, fontWeight: 600 }}>
                      测试结果：{testResult.connected ? "连接成功" : "连接失败"}
                    </span>
                  </div>
                  {testResult.connected && testResult.version && (
                    <p className="muted" style={{ fontSize: 13, margin: "4px 0 0 16px" }}>
                      aria2 版本: {testResult.version}
                    </p>
                  )}
                  {testResult.error && (
                    <p style={{ fontSize: 13, margin: "4px 0 0 16px", color: "#ff3b30" }}>
                      {testResult.error}
                    </p>
                  )}
                </div>
              )}
            </div>

            <h2 style={{ marginBottom: 24 }}>文件管理配置</h2>

            <div style={{ marginBottom: 32 }}>
              <label
                style={{ display: "block", marginBottom: 8, fontWeight: 600 }}
              >
                隐藏文件后缀名
              </label>
              <p className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                在文件管理页面隐藏指定后缀名的文件。输入后缀名（如 aria2 或
                .aria2）并按回车添加。
              </p>

              <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                <input
                  className="input"
                  type="text"
                  value={extensionInput}
                  onChange={(e) => setExtensionInput(e.target.value)}
                  onKeyDown={handleExtensionKeyDown}
                  placeholder="输入后缀名，按回车添加"
                  style={{ flex: 1 }}
                />
                <button
                  type="button"
                  className="button"
                  onClick={addExtension}
                  style={{ padding: "0 20px" }}
                >
                  添加
                </button>
              </div>

              <div style={{ marginBottom: 12 }}>
                <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                  常用后缀名：
                </p>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {[".aria2", ".tmp", ".part", ".download", ".crdownload"].map(
                    (ext) => (
                      <button
                        key={ext}
                        type="button"
                        onClick={() => addCommonExtension(ext)}
                        style={{
                          padding: "4px 12px",
                          fontSize: 12,
                          border: "1px solid rgba(0,0,0,0.1)",
                          borderRadius: 4,
                          background: "white",
                          cursor: "pointer",
                        }}
                      >
                        {ext}
                      </button>
                    ),
                  )}
                </div>
              </div>

              {hiddenExtensions.length > 0 && (
                <div
                  style={{
                    display: "flex",
                    gap: 8,
                    flexWrap: "wrap",
                    padding: 12,
                    background: "rgba(0,0,0,0.02)",
                    borderRadius: 6,
                  }}
                >
                  {hiddenExtensions.map((ext) => (
                    <div
                      key={ext}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "6px 12px",
                        background: "#0071e3",
                        color: "white",
                        borderRadius: 16,
                        fontSize: 14,
                      }}
                    >
                      <span>{ext}</span>
                      <button
                        type="button"
                        onClick={() => removeExtension(ext)}
                        style={{
                          background: "none",
                          border: "none",
                          color: "white",
                          cursor: "pointer",
                          padding: 0,
                          fontSize: 16,
                          lineHeight: 1,
                        }}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
              <button className="button" type="submit" disabled={saving}>
                {saving ? "保存中..." : "保存配置"}
              </button>
              {saveSuccess && (
                <span style={{ color: "#34c759", fontSize: 14, fontWeight: 500 }}>
                  ✓ 配置已保存
                </span>
              )}
            </div>
          </form>
        </div>
      </div>
    </AuthLayout>
  );
}
