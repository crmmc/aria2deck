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
      {icon && <span className="empty-state-icon">{icon}</span>}
      {title && <h2 className="empty-state-title">{title}</h2>}
      {description && <p className="empty-state-description">{description}</p>}
      {children}
    </div>
  );
}
