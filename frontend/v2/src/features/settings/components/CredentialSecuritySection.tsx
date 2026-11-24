import { Icon } from "@/shared/components/Icon";

type CredentialSecuritySectionProps = {
  invalidating: boolean;
  onInvalidate: () => void;
};

export function CredentialSecuritySection({
  invalidating,
  onInvalidate,
}: CredentialSecuritySectionProps) {
  return (
    <section className="panel danger-zone admin-settings-danger" aria-labelledby="settings-credential-security-title">
      <div className="panel-body">
        <div className="settings-section-head">
          <h2 id="settings-credential-security-title">作废全部凭证</h2>
          <p>删除所有 API Token 和用户 RPC Secret，当前登录会话不受影响。</p>
        </div>
        <button
          type="button"
          className="button danger"
          onClick={onInvalidate}
          disabled={invalidating}
          aria-label="作废全部 API Token 与 RPC Secret"
        >
          <Icon name="shield" />
          {invalidating ? "作废中..." : "作废全部凭证"}
        </button>
      </div>
    </section>
  );
}
