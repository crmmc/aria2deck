import type { RpcAccessResponse } from "@/shared/api/types";
import { Icon } from "@/shared/components/Icon";

type RpcAccessSectionProps = {
  rpcAccess: RpcAccessResponse | null;
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
  const enabled = rpcAccess?.enabled === true;
  return (
    <section className="panel profile-panel profile-panel-wide">
      <div className="panel-head"><h2>外部访问</h2></div>
      <div className="panel-body">
        <div className="switch-row">
          <div className="switch-copy">
            <strong><label htmlFor="profile-rpc-toggle">允许外部 aria2 客户端连接</label></strong>
            <p>开启后可使用 AriaNg、Motrix 等客户端管理当前用户的下载任务。</p>
          </div>
          <div className="switch-control">
            <span className="switch-state">{enabled ? "已启用" : "已停用"}</span>
            <button
              id="profile-rpc-toggle"
              type="button"
              onClick={() => onToggle(!enabled)}
              disabled={rpcLoading}
              className={`toggle-switch ${enabled ? "toggle-switch-on" : "toggle-switch-off"} ${rpcLoading ? "opacity-60 cursor-not-allowed" : ""}`}
              aria-label="允许外部 aria2 客户端连接"
            >
              <div className="toggle-knob" style={{ transform: enabled ? "translateX(20px)" : "none" }} />
            </button>
          </div>
        </div>

        {enabled && rpcAccess?.secret && (
          <>
            <div className="form-grid profile-rpc-grid">
              <div className="field">
                <span className="form-label field-label">RPC 地址</span>
                <div className="profile-secret">
                  <code className="code-block input mono">{rpcUrl}</code>
                  <button type="button" className="icon-btn" onClick={onCopyRpcUrl} title="复制 RPC 地址" aria-label={copiedUrl ? "已复制" : "复制 RPC 地址"}>
                    <Icon name={copiedUrl ? "check" : "copy"} className="icon-sm" />
                  </button>
                </div>
              </div>
              <div className="field">
                <span className="form-label field-label">RPC 密钥</span>
                <div className="profile-secret">
                  <code className="code-block input mono">{rpcAccess.secret}</code>
                  <button type="button" className="icon-btn" onClick={onCopySecret} title="复制 RPC 密钥" aria-label={copiedSecret ? "已复制" : "复制 RPC 密钥"}>
                    <Icon name={copiedSecret ? "check" : "copy"} className="icon-sm" />
                  </button>
                </div>
              </div>
            </div>
            <div className="profile-actions">
              <button type="button" className="button secondary btn-sm" onClick={onRefreshSecret} disabled={rpcLoading}>
                <Icon name="refresh" className="icon-sm" />刷新
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
