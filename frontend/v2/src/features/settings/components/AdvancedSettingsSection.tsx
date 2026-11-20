import { useId, useState, type ReactNode } from "react";

/** 归档参考 系统高级设置折叠区（reducer 之外的本地展开状态）。 */
export function AdvancedSettingsSection({ children }: { children: ReactNode }) {
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
