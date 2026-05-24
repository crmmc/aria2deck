"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import { MachineStats } from "@/types";
import { formatBytes, bytesToGB, gbToBytes } from "@/lib/utils";

function AdvancedSettingsSection({ children }: { children: React.ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  return (
    <section className={`settings-advanced-section mt-7${expanded ? " settings-advanced-section-open" : ""}`}>
      <button
        type="button"
        className="settings-advanced-toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <span className="settings-advanced-copy">
          <span className="settings-advanced-title">系统高级设置</span>
          <span className="settings-advanced-description">
            收纳接口频率限制和下载并发限制，按需展开调整。
          </span>
        </span>
        <span className="settings-advanced-meta">
          <span className="settings-advanced-state">{expanded ? "收起" : "展开"}</span>
          <span
            aria-hidden="true"
            className={`settings-advanced-chevron${expanded ? " settings-advanced-chevron-open" : ""}`}
          >
            ⌄
          </span>
        </span>
      </button>

      {expanded && (
        <div id={panelId} className="settings-advanced-panel">
          {children}
        </div>
      )}
    </section>
  );
}

export default function SettingsPage() {
  const { push } = useRouter();
  const { showToast } = useToast();
  const mountedRef = useRef(true);
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [machineStats, setMachineStats] = useState<MachineStats | null>(null);

  const [maxTaskSize, setMaxTaskSize] = useState("");
  const [minFreeDisk, setMinFreeDisk] = useState("");
  const [aria2RpcUrl, setAria2RpcUrl] = useState("");
  const [aria2RpcSecret, setAria2RpcSecret] = useState("");
  const [aria2BtStopTimeoutSeconds, setAria2BtStopTimeoutSeconds] = useState(7 * 24 * 60 * 60);
  const [hiddenExtensions, setHiddenExtensions] = useState<string[]>([]);
  const [extensionInput, setExtensionInput] = useState("");
  const [packFormat, setPackFormat] = useState<"zip" | "tar.zst">("zip");
  const [packCompressionLevel, setPackCompressionLevel] = useState(5);
  // WebSocket 重连配置
  const [wsReconnectMaxDelay, setWsReconnectMaxDelay] = useState(60);
  const [wsReconnectJitter, setWsReconnectJitter] = useState(0.2);
  const [wsReconnectFactor, setWsReconnectFactor] = useState(2);
  const [siteTitle, setSiteTitle] = useState("");
  // 请求频率限制
  const [rateLimitAccountSecurity, setRateLimitAccountSecurity] = useState(5);
  const [rateLimitAuthenticatedApi, setRateLimitAuthenticatedApi] = useState(60);
  const [rateLimitPublicApi, setRateLimitPublicApi] = useState(60);
  const [rateLimitShareAccess, setRateLimitShareAccess] = useState(5);
  const [rateLimitAuthenticatedDownload, setRateLimitAuthenticatedDownload] = useState(300);
  const [rateLimitAnonymousDownload, setRateLimitAnonymousDownload] = useState(60);
  const [rateLimitCreateTask, setRateLimitCreateTask] = useState(30);
  const [rateLimitCreateTorrent, setRateLimitCreateTorrent] = useState(20);
  const [rateLimitCreatePack, setRateLimitCreatePack] = useState(5);
  const [rateLimitAria2Test, setRateLimitAria2Test] = useState(10);
  const [rateLimitRpc, setRateLimitRpc] = useState(300);
  // 下载并发限制
  const [downloadTotalConnections, setDownloadTotalConnections] = useState(100);
  const [downloadAuthenticatedReservedConnections, setDownloadAuthenticatedReservedConnections] = useState(60);
  const [downloadAuthenticatedPerUserConnections, setDownloadAuthenticatedPerUserConnections] = useState(16);
  const [downloadAuthenticatedPerFileConnections, setDownloadAuthenticatedPerFileConnections] = useState(8);
  const [downloadAnonymousBaseConnections, setDownloadAnonymousBaseConnections] = useState(20);
  const [downloadAnonymousBorrowConnections, setDownloadAnonymousBorrowConnections] = useState(20);
  const [downloadAnonymousPerIpConnections, setDownloadAnonymousPerIpConnections] = useState(4);
  const [downloadAnonymousPerFileConnections, setDownloadAnonymousPerFileConnections] = useState(2);
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
    mountedRef.current = true;
    (async () => {
      try {
        const user = await api.me();
        if (!mountedRef.current) return;
        if (!user.is_admin) {
          push("/tasks");
          return;
        }
        setIsAdmin(true);
        await loadConfig();
      } catch {
        if (!mountedRef.current) return;
        setError("加载配置失败");
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    })();
    return () => {
      mountedRef.current = false;
    };
  }, [push]);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  async function loadConfig(throwOnError: boolean = false) {
    try {
      const [cfg, stats, aria2Ver] = await Promise.all([
        api.getConfig(),
        api.getMachineStats(),
        api.getAria2Version(),
      ]);
      if (!mountedRef.current) return;
      setMaxTaskSize(bytesToGB(cfg.max_task_size));
      setMinFreeDisk(bytesToGB(cfg.min_free_disk));
      setAria2RpcUrl(cfg.aria2_rpc_url || "");
      setAria2RpcSecret(cfg.aria2_rpc_secret || "");
      setAria2BtStopTimeoutSeconds(cfg.aria2_bt_stop_timeout_seconds ?? 7 * 24 * 60 * 60);
      setHiddenExtensions(cfg.hidden_file_extensions || []);
      setPackFormat(cfg.pack_format || "zip");
      setPackCompressionLevel(cfg.pack_compression_level ?? 5);
      setWsReconnectMaxDelay(cfg.ws_reconnect_max_delay ?? 60);
      setWsReconnectJitter(cfg.ws_reconnect_jitter ?? 0.2);
      setWsReconnectFactor(cfg.ws_reconnect_factor ?? 2);
      setSiteTitle(cfg.site_title || "");
      setRateLimitAccountSecurity(cfg.rate_limit_account_security ?? 5);
      setRateLimitAuthenticatedApi(cfg.rate_limit_authenticated_api ?? 60);
      setRateLimitPublicApi(cfg.rate_limit_public_api ?? 60);
      setRateLimitShareAccess(cfg.rate_limit_share_access ?? 5);
      setRateLimitAuthenticatedDownload(cfg.rate_limit_authenticated_download ?? 300);
      setRateLimitAnonymousDownload(cfg.rate_limit_anonymous_download ?? 60);
      setRateLimitCreateTask(cfg.rate_limit_create_task ?? 30);
      setRateLimitCreateTorrent(cfg.rate_limit_create_torrent ?? 20);
      setRateLimitCreatePack(cfg.rate_limit_create_pack ?? 5);
      setRateLimitAria2Test(cfg.rate_limit_aria2_test ?? 10);
      setRateLimitRpc(cfg.rate_limit_rpc ?? 300);
      setDownloadTotalConnections(cfg.download_total_connections ?? 100);
      setDownloadAuthenticatedReservedConnections(cfg.download_authenticated_reserved_connections ?? 60);
      setDownloadAuthenticatedPerUserConnections(cfg.download_authenticated_per_user_connections ?? 16);
      setDownloadAuthenticatedPerFileConnections(cfg.download_authenticated_per_file_connections ?? 8);
      setDownloadAnonymousBaseConnections(cfg.download_anonymous_base_connections ?? 20);
      setDownloadAnonymousBorrowConnections(cfg.download_anonymous_borrow_connections ?? 20);
      setDownloadAnonymousPerIpConnections(cfg.download_anonymous_per_ip_connections ?? 4);
      setDownloadAnonymousPerFileConnections(cfg.download_anonymous_per_file_connections ?? 2);
      setMachineStats(stats);
      setAria2Status(aria2Ver);
      setTestResult(null);
    } catch {
      if (!mountedRef.current) return;
      setError("加载配置失败");
      if (throwOnError) {
        throw new Error("加载配置失败");
      }
    }
  }

  async function saveConfig(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const maxTaskSizeGb = parseFloat(maxTaskSize);
      const minFreeDiskGb = parseFloat(minFreeDisk);
      if (!Number.isFinite(maxTaskSizeGb) || maxTaskSizeGb <= 0) {
        setSaveError("最大任务大小必须为正数");
        return;
      }
      if (!Number.isFinite(minFreeDiskGb) || minFreeDiskGb <= 0) {
        setSaveError("最小剩余磁盘空间必须为正数");
        return;
      }
      const newMax = gbToBytes(maxTaskSizeGb);
      const newMin = gbToBytes(minFreeDiskGb);
      if (isNaN(newMax) || isNaN(newMin) || newMax <= 0 || newMin <= 0) {
        setSaveError("请输入有效的数值");
        return;
      }

      await api.updateConfig({
        max_task_size: newMax,
        min_free_disk: newMin,
        aria2_rpc_url: aria2RpcUrl,
        aria2_rpc_secret: aria2RpcSecret.startsWith("*")
          ? undefined
          : aria2RpcSecret,
        aria2_bt_stop_timeout_seconds: aria2BtStopTimeoutSeconds,
        hidden_file_extensions: hiddenExtensions,
        pack_format: packFormat,
        pack_compression_level: packCompressionLevel,
        ws_reconnect_max_delay: wsReconnectMaxDelay,
        ws_reconnect_jitter: wsReconnectJitter,
        ws_reconnect_factor: wsReconnectFactor,
        site_title: siteTitle || undefined,
        rate_limit_account_security: rateLimitAccountSecurity,
        rate_limit_authenticated_api: rateLimitAuthenticatedApi,
        rate_limit_public_api: rateLimitPublicApi,
        rate_limit_share_access: rateLimitShareAccess,
        rate_limit_authenticated_download: rateLimitAuthenticatedDownload,
        rate_limit_anonymous_download: rateLimitAnonymousDownload,
        rate_limit_create_task: rateLimitCreateTask,
        rate_limit_create_torrent: rateLimitCreateTorrent,
        rate_limit_create_pack: rateLimitCreatePack,
        rate_limit_aria2_test: rateLimitAria2Test,
        rate_limit_rpc: rateLimitRpc,
        download_total_connections: downloadTotalConnections,
        download_authenticated_reserved_connections: downloadAuthenticatedReservedConnections,
        download_authenticated_per_user_connections: downloadAuthenticatedPerUserConnections,
        download_authenticated_per_file_connections: downloadAuthenticatedPerFileConnections,
        download_anonymous_base_connections: downloadAnonymousBaseConnections,
        download_anonymous_borrow_connections: downloadAnonymousBorrowConnections,
        download_anonymous_per_ip_connections: downloadAnonymousPerIpConnections,
        download_anonymous_per_file_connections: downloadAnonymousPerFileConnections,
      });

      await loadConfig(true);
      if (!mountedRef.current) return;
      showToast("配置已保存", "success");
    } catch (err) {
      if (!mountedRef.current) return;
      const message = (err as Error).message || "保存配置失败";
      setSaveError(message);
    } finally {
      if (mountedRef.current) setSaving(false);
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
      if (!mountedRef.current) return;
      setTestResult(result);
    } catch (err) {
      if (!mountedRef.current) return;
      setTestResult({ connected: false, error: (err as Error).message });
    } finally {
      if (mountedRef.current) setTestingConnection(false);
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

    setHiddenExtensions(prev => [...prev, withDot]);
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
      setHiddenExtensions(prev => [...prev, withDot]);
    }
  }

  const toPercent = (value: number, total: number) => {
    if (!total || total <= 0) return 0;
    return (value / total) * 100;
  };

  if (loading) return null;
  if (error) return (
    <div className="glass-frame full-height animate-in">
      <div className="card text-danger">{error}</div>
    </div>
  );
  if (!isAdmin) return null;

  return (
    <div className="glass-frame full-height animate-in">
      <div className="page-header">
        <h1 className="page-title">系统设置</h1>
        <p className="muted">系统配置（仅管理员）</p>
      </div>

      {machineStats && (
        <div className="card mb-6">
          <h2 className="mb-4">机器磁盘空间</h2>
          <div className="flex items-baseline gap-2">
            <span className="stats-value">{formatBytes(machineStats.disk_free)}</span>
            <span className="stats-unit">/ {formatBytes(machineStats.disk_total)}</span>
            <span className="muted">可用</span>
          </div>
          <div className="progress-container mt-2 max-w-600" style={{ overflow: "hidden" }}>
            <div style={{ display: "flex", width: "100%", height: "100%" }}>
              <div
                style={{
                  width: `${toPercent(machineStats.download_used, machineStats.disk_total)}%`,
                  background: "#3b82f6",
                }}
              />
              <div
                style={{
                  width: `${toPercent(machineStats.system_used, machineStats.disk_total)}%`,
                  background: "#f59e0b",
                }}
              />
            </div>
          </div>
          <div className="mt-3 text-sm" style={{ display: "grid", gap: 6 }}>
            <div className="flex items-center gap-2">
              <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#3b82f6" }} />
              <span>下载占用：{formatBytes(machineStats.download_used)}（{toPercent(machineStats.download_used, machineStats.disk_total).toFixed(1)}%）</span>
            </div>
            <div className="flex items-center gap-2">
              <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#f59e0b" }} />
              <span>系统占用：{formatBytes(machineStats.system_used)}（{toPercent(machineStats.system_used, machineStats.disk_total).toFixed(1)}%）</span>
            </div>
            <div className="muted">总占用：{formatBytes(machineStats.disk_used)}（{toPercent(machineStats.disk_used, machineStats.disk_total).toFixed(1)}%）</div>
          </div>
        </div>
      )}

      <div className="card">
        <form onSubmit={saveConfig} className="max-w-600">
          <h2 className="section-title">系统配置</h2>

          <div className="mb-6">
            <label className="form-label-lg" htmlFor="settings-site-title">网站标题</label>
            <p className="muted text-sm mb-2">自定义网站标题，显示在侧边栏和页面标题中。留空使用默认值。</p>
            <input
              id="settings-site-title"
              aria-label="网站标题"
              className="input"
              type="text"
              value={siteTitle}
              onChange={(e) => setSiteTitle(e.target.value)}
              placeholder="Aria2 控制器"
              maxLength={50}
            />
          </div>

          <div className="mb-6">
            <label className="form-label-lg" htmlFor="settings-max-task-size">最大任务大小 (GB)</label>
            <p className="muted text-sm mb-2">超过此大小的任务将被拒绝。</p>
            <input
              id="settings-max-task-size"
              aria-label="最大任务大小 GB"
              className="input"
              type="number"
              step="any"
              min="0.1"
              value={maxTaskSize}
              onChange={(e) => setMaxTaskSize(e.target.value)}
            />
          </div>

          <div className="mb-7">
            <label className="form-label-lg" htmlFor="settings-min-disk-space">最小剩余磁盘空间 (GB)</label>
            <p className="muted text-sm mb-2">如果剩余空间低于此值，将停止接受新任务。</p>
            <input
              id="settings-min-disk-space"
              aria-label="最小剩余磁盘空间 GB"
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
            <label className="form-label-lg" htmlFor="settings-rpc-url">aria2 RPC URL</label>
            <p className="muted text-sm mb-2">aria2 JSON-RPC 接口地址，例如：http://localhost:6800/jsonrpc</p>
            <input
              id="settings-rpc-url"
              aria-label="aria2 RPC URL"
              className="input"
              type="text"
              value={aria2RpcUrl}
              onChange={(e) => setAria2RpcUrl(e.target.value)}
              placeholder="http://localhost:6800/jsonrpc"
            />
          </div>

          <div className="mb-4">
            <label className="form-label-lg" htmlFor="settings-rpc-secret">aria2 RPC Secret</label>
            <p className="muted text-sm mb-2">aria2 RPC 认证密钥（可选）。留空表示不使用认证。</p>
            <input
              id="settings-rpc-secret"
              aria-label="aria2 RPC Secret"
              className="input"
              type="password"
              value={aria2RpcSecret}
              onChange={(e) => setAria2RpcSecret(e.target.value)}
              placeholder="留空表示无密钥"
            />
          </div>

          <div className="mb-6">
            <label className="form-label-lg" htmlFor="settings-bt-stop-timeout">BT 无数据停止超时 (秒)</label>
            <p className="muted text-sm mb-2">创建 aria2 任务时写入 bt-stop-timeout，默认 604800 秒（7 天），0 表示禁用。</p>
            <input
              id="settings-bt-stop-timeout"
              aria-label="BT 无数据停止超时秒数"
              className="input"
              type="number"
              min="0"
              max={365 * 24 * 60 * 60}
              value={aria2BtStopTimeoutSeconds}
              onChange={(e) => setAria2BtStopTimeoutSeconds(Math.max(0, parseInt(e.target.value) || 0))}
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
            <label className="form-label-lg" htmlFor="settings-hidden-extensions">隐藏文件后缀名</label>
            <p className="muted text-sm mb-2">
              在文件管理页面隐藏指定后缀名的文件。输入后缀名（如 aria2 或 .aria2）并按回车添加。
            </p>

            <div className="flex gap-2 mb-3">
              <input
                id="settings-hidden-extensions"
                aria-label="隐藏文件后缀名"
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
            <label className="form-label-lg" htmlFor="settings-pack-format">打包格式</label>
            <p className="muted text-sm mb-3">选择文件夹打包的压缩格式。</p>
            <div className="flex gap-4">
              <label className="checkbox-label">
                <input
                  id="settings-pack-format"
                  aria-labelledby="settings-pack-format-zip-label"
                  type="radio"
                  name="packFormat"
                  value="zip"
                  checked={packFormat === "zip"}
                  onChange={() => setPackFormat("zip")}
                />
                <span id="settings-pack-format-zip-label">ZIP（Deflate64）</span>
              </label>
              <label className="checkbox-label">
                <input
                  aria-labelledby="settings-pack-format-tar-zst-label"
                  type="radio"
                  name="packFormat"
                  value="tar.zst"
                  checked={packFormat === "tar.zst"}
                  onChange={() => setPackFormat("tar.zst")}
                />
                <span id="settings-pack-format-tar-zst-label">TAR + Zstandard</span>
              </label>
            </div>
          </div>

          <div className="mb-7">
            <label className="form-label-lg" htmlFor="settings-pack-compression-level">压缩等级: {packCompressionLevel}</label>
            <p className="muted text-sm mb-3">
              {packFormat === "zip"
                ? "ZIP: 0 = 仅打包不压缩, 1 = 最快, 9 = 最慢/最小体积"
                : "TAR+Zstandard: 0-9 会映射到 zstd 速度/压缩率档位"}
            </p>
            <input
              id="settings-pack-compression-level"
              aria-label="打包压缩等级"
              type="range"
              min="0"
              max="9"
              value={packCompressionLevel}
              onChange={(e) => setPackCompressionLevel(parseInt(e.target.value))}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <h2 className="section-title mt-7">WebSocket 重连设置</h2>
          <p className="muted text-sm mb-4">配置后端与 aria2 WebSocket 连接断开后的重连策略。</p>

          <div className="mb-6">
            <label className="form-label-lg" htmlFor="settings-ws-max-delay">最大重连延迟: {wsReconnectMaxDelay} 秒</label>
            <p className="muted text-sm mb-3">指数退避的最大等待时间（1-300 秒）</p>
            <input
              id="settings-ws-max-delay"
              aria-label="最大重连延迟"
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
            <label className="form-label-lg" htmlFor="settings-ws-jitter">抖动系数: {(wsReconnectJitter * 100).toFixed(0)}%</label>
            <p className="muted text-sm mb-3">重连延迟的随机波动范围（0-100%）</p>
            <input
              id="settings-ws-jitter"
              aria-label="重连抖动系数"
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
            <label className="form-label-lg" htmlFor="settings-ws-factor">指数因子: {wsReconnectFactor.toFixed(1)}</label>
            <p className="muted text-sm mb-3">每次重连延迟的倍增系数（1.1-10）</p>
            <input
              id="settings-ws-factor"
              aria-label="重连指数因子"
              type="range"
              min="11"
              max="100"
              value={wsReconnectFactor * 10}
              onChange={(e) => setWsReconnectFactor(parseInt(e.target.value) / 10)}
              className="w-full"
              style={{ maxWidth: 300 }}
            />
          </div>

          <AdvancedSettingsSection>
            <h2 className="section-title">接口频率限制</h2>
            <p className="muted text-sm mb-4">限制用户在单位时间内的请求次数，修改后即时生效。</p>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-account-security">账户安全限流</label>
              <p className="muted text-sm mb-3">每 5 分钟最大尝试次数（登录、首个用户创建、修改密码）</p>
              <input
                id="settings-rate-account-security"
                aria-label="账户安全限流"
                type="number"
                min="1"
                max="100"
                value={rateLimitAccountSecurity}
                onChange={(e) => setRateLimitAccountSecurity(Math.max(1, Math.min(100, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-auth-api">普通已登录 API 限流</label>
              <p className="muted text-sm mb-3">每分钟最大查询请求次数（0 = 不限制）</p>
              <input
                id="settings-rate-auth-api"
                aria-label="普通已登录 API 限流"
                type="number"
                min="0"
                max="10000"
                value={rateLimitAuthenticatedApi}
                onChange={(e) => setRateLimitAuthenticatedApi(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-public-api">普通匿名公开 API 限流</label>
              <p className="muted text-sm mb-3">每分钟最大公开查询次数（0 = 不限制）</p>
              <input
                id="settings-rate-public-api"
                aria-label="普通匿名公开 API 限流"
                type="number"
                min="0"
                max="10000"
                value={rateLimitPublicApi}
                onChange={(e) => setRateLimitPublicApi(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-share-access">分享密码验证限流</label>
              <p className="muted text-sm mb-3">每分钟最大密码验证次数</p>
              <input
                id="settings-rate-share-access"
                aria-label="分享密码验证限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitShareAccess}
                onChange={(e) => setRateLimitShareAccess(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-auth-download">已登录下载限流</label>
              <p className="muted text-sm mb-3">每分钟最大请求次数（0 = 不限制）</p>
              <input
                id="settings-rate-auth-download"
                aria-label="已登录下载限流"
                type="number"
                min="0"
                max="10000"
                value={rateLimitAuthenticatedDownload}
                onChange={(e) => setRateLimitAuthenticatedDownload(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-anon-download">匿名下载限流</label>
              <p className="muted text-sm mb-3">每分钟最大匿名下载请求次数（0 = 不限制）</p>
              <input
                id="settings-rate-anon-download"
                aria-label="匿名下载限流"
                type="number"
                min="0"
                max="10000"
                value={rateLimitAnonymousDownload}
                onChange={(e) => setRateLimitAnonymousDownload(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-create-task">创建任务限流</label>
              <p className="muted text-sm mb-3">每分钟最大创建任务次数</p>
              <input
                id="settings-rate-create-task"
                aria-label="创建任务限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitCreateTask}
                onChange={(e) => setRateLimitCreateTask(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-create-torrent">创建种子限流</label>
              <p className="muted text-sm mb-3">每分钟最大上传种子次数</p>
              <input
                id="settings-rate-create-torrent"
                aria-label="创建种子限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitCreateTorrent}
                onChange={(e) => setRateLimitCreateTorrent(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-create-pack">创建打包限流</label>
              <p className="muted text-sm mb-3">每分钟最大创建打包次数</p>
              <input
                id="settings-rate-create-pack"
                aria-label="创建打包限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitCreatePack}
                onChange={(e) => setRateLimitCreatePack(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-aria2-test">aria2 测试限流</label>
              <p className="muted text-sm mb-3">每分钟最大连接测试次数</p>
              <input
                id="settings-rate-aria2-test"
                aria-label="aria2 测试限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitAria2Test}
                onChange={(e) => setRateLimitAria2Test(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-rate-rpc">JSON-RPC 限流</label>
              <p className="muted text-sm mb-3">每分钟最大 RPC 请求次数</p>
              <input
                id="settings-rate-rpc"
                aria-label="JSON-RPC 限流"
                type="number"
                min="1"
                max="10000"
                value={rateLimitRpc}
                onChange={(e) => setRateLimitRpc(Math.max(1, Math.min(10000, parseInt(e.target.value) || 1)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>

            <h2 className="section-title mt-7">下载并发限制</h2>
            <p className="muted text-sm mb-4">控制已登录下载保底与匿名分享下载的可借用容量，保证已登录用户体验优先。</p>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-total">系统总下载连接上限</label>
              <p className="muted text-sm mb-3">所有用户的总并发下载连接数上限（0 = 不限制）</p>
              <input
                id="settings-conn-total"
                aria-label="系统总下载连接上限"
                type="number"
                min="0"
                max="10000"
                value={downloadTotalConnections}
                onChange={(e) => setDownloadTotalConnections(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-auth-reserved">已登录保底连接数</label>
              <p className="muted text-sm mb-3">为已登录下载预留的最小可用连接数</p>
              <input
                id="settings-conn-auth-reserved"
                aria-label="已登录保底连接数"
                type="number"
                min="0"
                max="10000"
                value={downloadAuthenticatedReservedConnections}
                onChange={(e) => setDownloadAuthenticatedReservedConnections(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-user-max">已登录单用户最大并发</label>
              <p className="muted text-sm mb-3">单个已登录用户的并发下载连接数上限（0 = 不限制）</p>
              <input
                id="settings-conn-user-max"
                aria-label="已登录单用户最大并发"
                type="number"
                min="0"
                max="1000"
                value={downloadAuthenticatedPerUserConnections}
                onChange={(e) => setDownloadAuthenticatedPerUserConnections(Math.max(0, Math.min(1000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-auth-per-file">已登录单文件最大并发</label>
              <p className="muted text-sm mb-3">同一已登录用户对同一文件的并发下载连接数上限（0 = 不限制）</p>
              <input
                id="settings-conn-auth-per-file"
                aria-label="已登录单文件最大并发"
                type="number"
                min="0"
                max="100"
                value={downloadAuthenticatedPerFileConnections}
                onChange={(e) => setDownloadAuthenticatedPerFileConnections(Math.max(0, Math.min(100, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-anon-base">匿名基础连接数</label>
              <p className="muted text-sm mb-3">匿名分享下载默认可占用的连接数</p>
              <input
                id="settings-conn-anon-base"
                aria-label="匿名基础连接数"
                type="number"
                min="0"
                max="10000"
                value={downloadAnonymousBaseConnections}
                onChange={(e) => setDownloadAnonymousBaseConnections(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-anon-borrow">匿名可借用连接数</label>
              <p className="muted text-sm mb-3">匿名分享在不影响已登录保底的前提下可额外借用的连接数</p>
              <input
                id="settings-conn-anon-borrow"
                aria-label="匿名可借用连接数"
                type="number"
                min="0"
                max="10000"
                value={downloadAnonymousBorrowConnections}
                onChange={(e) => setDownloadAnonymousBorrowConnections(Math.max(0, Math.min(10000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-anon-per-ip">匿名单 IP 最大并发</label>
              <p className="muted text-sm mb-3">单个匿名来源的并发下载连接数上限（0 = 不限制）</p>
              <input
                id="settings-conn-anon-per-ip"
                aria-label="匿名单 IP 最大并发"
                type="number"
                min="0"
                max="1000"
                value={downloadAnonymousPerIpConnections}
                onChange={(e) => setDownloadAnonymousPerIpConnections(Math.max(0, Math.min(1000, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
            <div className="mb-7">
              <label className="form-label-lg" htmlFor="settings-conn-anon-per-file">匿名单文件最大并发</label>
              <p className="muted text-sm mb-3">同一匿名来源对同一文件的并发下载连接数上限（0 = 不限制）</p>
              <input
                id="settings-conn-anon-per-file"
                aria-label="匿名单文件最大并发"
                type="number"
                min="0"
                max="100"
                value={downloadAnonymousPerFileConnections}
                onChange={(e) => setDownloadAnonymousPerFileConnections(Math.max(0, Math.min(100, parseInt(e.target.value) || 0)))}
                className="input"
                style={{ maxWidth: 200 }}
              />
            </div>
          </AdvancedSettingsSection>

          <div className="settings-form-actions flex items-center gap-4">
            <button className="button" type="submit" disabled={saving}>
              {saving ? "保存中..." : "保存配置"}
            </button>
            {saveError && (
              <span className="save-error-inline">{saveError}</span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
