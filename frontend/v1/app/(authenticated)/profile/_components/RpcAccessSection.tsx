import type { RpcAccessStatus } from "@/types";

type RpcAccessSectionProps = {
  rpcAccess: RpcAccessStatus | null;
  rpcLoading: boolean;
  copiedSecret: boolean;
  copiedUrl: boolean;
  rpcUrl: string;
  onToggle: (enabled: boolean) => void;
  onRefreshSecret: () => void;
  onCopySecret: () => void;
  onCopyRpcUrl: () => void;
};

export function RpcAccessSection({
  rpcAccess,
  rpcLoading,
  copiedSecret,
  copiedUrl,
  rpcUrl,
  onToggle,
  onRefreshSecret,
  onCopySecret,
  onCopyRpcUrl,
}: RpcAccessSectionProps) {
  return (
    <div className="card">
      <h2 className="section-title">外部访问</h2>

      <div className="max-w-600">
        <div className={`bg-black-02 rounded-lg p-4 ${rpcAccess?.enabled ? "mb-6" : ""}`}>
          <div className="flex-between mb-2">
            <label className="font-semibold" htmlFor="profile-rpc-toggle">允许外部 aria2 客户端连接</label>
            <button
              id="profile-rpc-toggle"
              type="button"
              onClick={() => onToggle(!rpcAccess?.enabled)}
              disabled={rpcLoading}
              className={`toggle-switch ${rpcAccess?.enabled ? "toggle-switch-on" : "toggle-switch-off"} ${rpcLoading ? "opacity-60 cursor-not-allowed" : ""}`}
              aria-label="允许外部 aria2 客户端连接"
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
                <span className="font-semibold text-base">RPC 密钥</span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="button secondary btn-sm"
                    onClick={onCopySecret}
                  >
                    {copiedSecret ? "已复制" : "复制"}
                  </button>
                  <button
                    type="button"
                    className="button secondary btn-sm"
                    onClick={onRefreshSecret}
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
                <span className="font-semibold text-base">RPC 地址</span>
                <button
                  type="button"
                  className="button secondary btn-sm"
                  onClick={onCopyRpcUrl}
                >
                  {copiedUrl ? "已复制" : "复制"}
                </button>
              </div>
              <code className="code-block">{rpcUrl}</code>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
