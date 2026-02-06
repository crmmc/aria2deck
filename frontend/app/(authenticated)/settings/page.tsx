"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { MachineStats } from "@/types";
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
  const [packFormat, setPackFormat] = useState<"zip" | "7z">("zip");
  const [packCompressionLevel, setPackCompressionLevel] = useState(5);
  const [packExtraArgs, setPackExtraArgs] = useState("");
  // WebSocket 重连配置
  const [wsReconnectMaxDelay, setWsReconnectMaxDelay] = useState(60);
  const [wsReconnectJitter, setWsReconnectJitter] = useState(0.2);
  const [wsReconnectFactor, setWsReconnectFactor] = useState(2);
  // 下载链接 Token 有效期
  const [downloadTokenExpiry, setDownloadTokenExpiry] = useState(7200);
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
      .catch(() => {
        setError("加载配置失败");
        setLoading(false);
      });
  }, [router]);

  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

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
      setPackFormat(cfg.pack_format || "zip");
      setPackCompressionLevel(cfg.pack_compression_level || 5);
      setPackExtraArgs(cfg.pack_extra_args || "");
      setWsReconnectMaxDelay(cfg.ws_reconnect_max_delay || 60);
      setWsReconnectJitter(cfg.ws_reconnect_jitter || 0.2);
      setWsReconnectFactor(cfg.ws_reconnect_factor || 2);
      setDownloadTokenExpiry(cfg.download_token_expiry || 7200);
      setMachineStats(stats);
      setAria2Status(aria2Ver);
      setTestResult(null);
    } catch {
      setError("加载配置失败");
    }
  }

  async function saveConfig(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveSuccess(false);
    setSaveError(null);
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
        pack_format: packFormat,
        pack_compression_level: packCompressionLevel,
        pack_extra_args: packExtraArgs,
        ws_reconnect_max_delay: wsReconnectMaxDelay,
        ws_reconnect_jitter: wsReconnectJitter,
        ws_reconnect_factor: wsReconnectFactor,
        download_token_expiry: downloadTokenExpiry,
      });

      await loadConfig();
      setSaveSuccess(true);
      setTimeout(() => setSaveSuccess(false), 3000);
    } catch (err) {
      const message = (err as Error).message || "保存配置失败";
      setSaveError(message);
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
    setTestResult(null);
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

    const normalized = ext.startsWith(".") ? ext.substring(1) : ext;

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

  const getDiskColor = (percent: number) => {
    if (percent >= 80) return "var(--danger)";
    if (percent >= 50) return "var(--warning)";
    return "var(--success)";
  };

  if (loading) return null;

  return (
    <div className="glass-frame full-height animate-in">
      <div className="page-header">
        <h1 className="page-title">系统设置</h1>
        <p className="muted">系统配置（仅管理员）</p>
      </div>

      {error && (
        <div className="card text-danger">{error}</div>
      )}

      {machineStats && (
        <div className="card mb-6">
          <h2 className="mb-4">机器磁盘空间</h2>
          <div className="flex items-baseline gap-2">
            <span className="stats-value">{formatBytes(machineStats.disk_free)}</span>
            <span className="stats-unit">/ {formatBytes(machineStats.disk_total)}</span>
            <span className="muted">可用</span>
          </div>
          <div className="progress-container mt-2 max-w-600">
            <div
              className="progress-bar"
              style={{
                width: `${(machineStats.disk_used / machineStats.disk_total) * 100}%`,
                background: getDiskColor((machineStats.disk_used / machineStats.disk_total) * 100),
              }}
            />
          </div>
        </div>
      )}

      <div className="card">
        <form onSubmit={saveConfig} className="max-w-600">
          <h2 className="section-title">系统配置</h2>

          <div className="mb-6">
            <label className="form-label-lg">最大任务大小 (GB)</label>
            <p className="muted text-sm mb-2">超过此大小的任务将被拒绝。</p>
            <input
              className="input"
              type="number"
              step="any"
              min="0.1"
              value={maxTaskSize}
              onChange={(e) => setMaxTaskSize(e.target.value)}
            />
          </div>

          <div className="mb-7">
            <label className="form-label-lg">最小剩余磁盘空间 (GB)</label>
            <p className="muted text-sm mb-2">如果剩余空间低于此值，将停止接受新任务。</p>
            <input
              className="input"
              type="number"
              step="any"
              min="0.1"
              value={minFreeDisk}
              onChange={(e) => setMinFreeDisk(e.target.value)}
            />
          </div>

          <h2 className="section-title">aria2 后端配置</h2>

          <div className={`mb-6 p-4 rounded-lg ${aria2Status?.connected ? "alert-success" : "alert-danger"}`}>
            <div className="flex-between mb-2">
              <div className="flex items-center gap-2">
                <div className={`status-dot ${aria2Status?.connected ? "status-dot-success" : "status-dot-danger"}`} />
                <span className="font-semibold">{aria2Status?.connected ? "已连接" : "未连接"}</span>
              </div>
              {aria2Status?.connected && aria2Status.version && (
                <span className="muted text-sm font-mono">aria2 {aria2Status.version}</span>
              )}
            </div>
            {aria2Status?.error && (
              <p className="muted text-sm text-danger">错误：{aria2Status.error}</p>
            )}
          </div>

          <div className="mb-6">
            <label className="form-label-lg">aria2 RPC URL</label>
            <p className="muted text-sm mb-2">aria2 JSON-RPC 接口地址，例如：http://localhost:6800/jsonrpc</p>
            <input
              className="input"
              type="text"
              value={aria2RpcUrl}
              onChange={(e) => setAria2RpcUrl(e.target.value)}
              placeholder="http://localhost:6800/jsonrpc"
            />
          </div>

          <div className="mb-4">
            <label className="form-label-lg">aria2 RPC Secret</label>
            <p className="muted text-sm mb-2">aria2 RPC 认证密钥（可选）。留空表示不使用认证。</p>
            <input
              className="input"
              type="password"
              value={aria2RpcSecret}
              onChange={(e) => setAria2RpcSecret(e.target.value)}
              placeholder="留空表示无密钥"
            />
          </div>

          <div className="mb-7">
            <button
              type="button"
              className="button secondary mb-4"
              onClick={testConnection}
              disabled={testingConnection}
            >
              {testingConnection ? "测试中..." : "测试连接"}
            </button>

            {testResult && (
              <div className={`p-3 rounded-lg ${testResult.connected ? "alert-success" : "alert-danger"}`}>
                <div className="flex items-center gap-2">
                  <div className={`status-dot-sm ${testResult.connected ? "status-dot-success" : "status-dot-danger"}`} />
                  <span className="text-base font-semibold">
                    测试结果：{testResult.connected ? "连接成功" : "连接失败"}
                  </span>
                </div>
                {testResult.connected && testResult.version && (
                  <p className="muted text-sm ml-4 mt-1">aria2 版本: {testResult.version}</p>
                )}
                {testResult.error && (
                  <p className="text-sm ml-4 mt-1 text-danger">{testResult.error}</p>
                )}
              </div>
            )}
          </div>

          <h2 className="section-title">文件管理配置</h2>

          <div className="mb-7">
            <label className="form-label-lg">隐藏文件后缀名</label>
            <p className="muted text-sm mb-2">
              在文件管理页面隐藏指定后缀名的文件。输入后缀名（如 aria2 或 .aria2）并按回车添加。
            </p>

            <div className="flex gap-2 mb-3">
              <input
                className="input flex-1"
                type="text"
                value={extensionInput}
                onChange={(e) => setExtensionInput(e.target.value)}
                onKeyDown={handleExtensionKeyDown}
                placeholder="输入后缀名，按回车添加"
              />
              <button type="button" className="button px-4" onClick={addExtension}>
                添加
              </button>
            </div>

            <div className="mb-3">
              <p className="muted text-xs mb-2">常用后缀名：</p>
              <div className="flex gap-2 flex-wrap">
                {[".aria2", ".tmp", ".part", ".download", ".crdownload"].map((ext) => (
                  <button
                    key={ext}
                    type="button"
                    onClick={() => addCommonExtension(ext)}
                    className="ext-btn"
                  >
                    {ext}
                  </button>
                ))}
              </div>
            </div>

            {hiddenExtensions.length > 0 && (
              <div className="flex gap-2 flex-wrap p-3 bg-black-02 rounded">
                {hiddenExtensions.map((ext) => (
                  <div key={ext} className="chip">
                    <span>{ext}</span>
                    <button
                      type="button"
                      onClick={() => removeExtension(ext)}
                      className="chip-close"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <h2 className="section-title mt-7">打包设置</h2>

          <div className="mb-6">
            <label className="form-label-lg">打包格式</label>
            <p className="muted text-sm mb-3">选择文件夹打包的压缩格式。</p>
            <div className="flex gap-4">
              <label className="checkbox-label">
                <input
                  type="radio"
                  name="packFormat"
                  value="zip"
                  checked={packFormat === "zip"}
                  onChange={() => setPackFormat("zip")}
                />
                <span>ZIP</span>
              </label>
              <label className="checkbox-label">
                <input
                  type="radio"
                  name="packFormat"
                  value="7z"
                  checked={packFormat === "7z"}
                  onChange={() => setPackFormat("7z")}
                />
                <span>7Z</span>
              </label>
            </div>
          </div>

          <div className="mb-7">
            <label className="form-label-lg">压缩等级: {packCompressionLevel}</label>
            <p className="muted text-sm mb-3">1 = 最快/最大体积, 9 = 最慢/最小体积</p>
            <input
              type="range"
              min="1"
              max="9"
              value={packCompressionLevel}
              onChange={(e) => setPackCompressionLevel(parseInt(e.target.value))}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <div className="mb-7">
            <label className="form-label-lg">7za 附加参数</label>
            <p className="muted text-sm mb-2">自定义 7za 命令参数，如 -mmt=2 限制 CPU 核心数</p>
            <input
              className="input"
              type="text"
              value={packExtraArgs}
              onChange={(e) => setPackExtraArgs(e.target.value)}
              placeholder="-mmt=2"
            />
          </div>

          <h2 className="section-title mt-7">WebSocket 重连设置</h2>
          <p className="muted text-sm mb-4">配置后端与 aria2 WebSocket 连接断开后的重连策略。</p>

          <div className="mb-6">
            <label className="form-label-lg">最大重连延迟: {wsReconnectMaxDelay} 秒</label>
            <p className="muted text-sm mb-3">指数退避的最大等待时间（1-300 秒）</p>
            <input
              type="range"
              min="1"
              max="300"
              value={wsReconnectMaxDelay}
              onChange={(e) => setWsReconnectMaxDelay(parseInt(e.target.value))}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <div className="mb-6">
            <label className="form-label-lg">抖动系数: {(wsReconnectJitter * 100).toFixed(0)}%</label>
            <p className="muted text-sm mb-3">重连延迟的随机波动范围（0-100%）</p>
            <input
              type="range"
              min="0"
              max="100"
              value={wsReconnectJitter * 100}
              onChange={(e) => setWsReconnectJitter(parseInt(e.target.value) / 100)}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <div className="mb-7">
            <label className="form-label-lg">指数因子: {wsReconnectFactor.toFixed(1)}</label>
            <p className="muted text-sm mb-3">每次重连延迟的倍增系数（1.1-10）</p>
            <input
              type="range"
              min="11"
              max="100"
              value={wsReconnectFactor * 10}
              onChange={(e) => setWsReconnectFactor(parseInt(e.target.value) / 10)}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <h2 className="section-title mt-7">下载链接设置</h2>

          <div className="mb-7">
            <label className="form-label-lg">下载链接有效期: {downloadTokenExpiry >= 3600 ? `${(downloadTokenExpiry / 3600).toFixed(1)} 小时` : `${Math.round(downloadTokenExpiry / 60)} 分钟`}</label>
            <p className="muted text-sm mb-3">文件下载链接的有效时间（1 分钟 - 7 天），过期后需重新获取</p>
            <input
              type="range"
              min="60"
              max="604800"
              step="60"
              value={downloadTokenExpiry}
              onChange={(e) => setDownloadTokenExpiry(parseInt(e.target.value))}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
            <div className="flex gap-2 mt-2">
              {[
                { label: "1小时", value: 3600 },
                { label: "2小时", value: 7200 },
                { label: "6小时", value: 21600 },
                { label: "24小时", value: 86400 },
              ].map((preset) => (
                <button
                  key={preset.value}
                  type="button"
                  onClick={() => setDownloadTokenExpiry(preset.value)}
                  className={`ext-btn ${downloadTokenExpiry === preset.value ? "ext-btn-active" : ""}`}
                >
                  {preset.label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-4">
            <button className="button" type="submit" disabled={saving}>
              {saving ? "保存中..." : "保存配置"}
            </button>
            {saveSuccess && (
              <span className="text-success text-base font-medium">✓ 配置已保存</span>
            )}
            {saveError && (
              <span className="save-error-inline">{saveError}</span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
