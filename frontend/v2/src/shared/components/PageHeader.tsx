import type { ReactNode } from "react";

import { Icon } from "@/shared/components/Icon";
import { openCommandPalette } from "@/shared/lib/commands";

type PageHeaderProps = {
  title: string;
  description: string;
  actions?: ReactNode;
};

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="page-header space-between">
      <div>
        <h1 className="page-title" id="page-title" tabIndex={-1}>{title}</h1>
        <p className="muted">{description}</p>
      </div>
      <div className="page-actions">
        <button
          className="icon-btn glass-command"
          type="button"
          data-action="open-command"
          aria-label="打开快捷操作"
          aria-keyshortcuts="Control+K Meta+K"
          title="快捷操作"
          onClick={(event) => openCommandPalette(event.currentTarget)}
        >
          <Icon name="command" />
        </button>
        {actions}
      </div>
    </div>
  );
}
