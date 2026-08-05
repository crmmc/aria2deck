type CredentialSecuritySectionProps = {
  invalidating: boolean;
  onInvalidate: () => void;
};

export function CredentialSecuritySection({
  invalidating,
  onInvalidate,
}: CredentialSecuritySectionProps) {
  return (
    <section className="mt-7" aria-labelledby="settings-credential-security-title">
      <h2 id="settings-credential-security-title" className="section-title">
        凭证安全
      </h2>
      <p className="muted text-sm mb-4">
        轮换 <code>ARIA2DECK_CREDENTIAL_PEPPER</code> 后，
        旧 API Token / 用户 RPC Secret 摘要会无法再验证。这里可一次性作废全部摘要，
        登录密码与当前管理员会话不受影响；用户需重新签发 Token 和 RPC Secret。
      </p>
      <button
        type="button"
        className="button danger"
        onClick={onInvalidate}
        disabled={invalidating}
        aria-label="作废全部 API Token 与 RPC Secret"
      >
        {invalidating ? "作废中..." : "作废全部 API Token / RPC Secret"}
      </button>
    </section>
  );
}
