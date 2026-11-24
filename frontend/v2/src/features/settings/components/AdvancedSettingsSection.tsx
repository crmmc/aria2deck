import { useId, useState, type ReactNode } from "react";

import { Icon } from "@/shared/components/Icon";

export function AdvancedSettingsSection({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  return (
    <section
      className={`settings-advanced-section admin-settings-advanced mt-7${expanded ? " settings-advanced-section-open" : ""}`}
    >
      <button
        type="button"
        className="settings-advanced-toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((previous) => !previous)}
      >
        <span className="settings-advanced-copy">
          <span className="settings-advanced-title">系统高级设置</span>
          <span className="settings-advanced-description">
            其余九项接口限流与六项下载连接限制，按需展开调整。
          </span>
        </span>
        <span className="settings-advanced-meta">
          <span className="settings-advanced-state">{expanded ? "收起" : "展开"}</span>
          <Icon
            name="chevron-down"
            className={`settings-advanced-chevron${expanded ? " settings-advanced-chevron-open" : ""}`}
          />
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
