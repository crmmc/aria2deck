import type { ReactNode } from "react";

type EmptyStateProps = {
  icon?: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
};

export function EmptyState({ icon, title, description, children }: EmptyStateProps) {
  return (
    <div className="empty-state">
      {icon && <div className="empty-state-icon">{icon}</div>}
      {title && <p className="font-medium mb-1">{title}</p>}
      {description && <p className="muted text-base">{description}</p>}
      {children}
    </div>
  );
}
