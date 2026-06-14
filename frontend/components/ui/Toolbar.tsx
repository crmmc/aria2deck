import type { ReactNode } from "react";

type ToolbarShellProps = {
  children: ReactNode;
  className?: string;
};

type ToolbarGroupProps = {
  children: ReactNode;
  className: string;
};

type ToolbarSearchInputProps = {
  value: string;
  placeholder: string;
  ariaLabel: string;
  onChange: (value: string) => void;
};

export function ToolbarShell({ children, className = "" }: ToolbarShellProps) {
  return (
    <div className={`card filter-toolbar inline-filter-toolbar${className ? ` ${className}` : ""}`}>
      {children}
    </div>
  );
}

export function ToolbarGroup({ children, className }: ToolbarGroupProps) {
  return <div className={`filter-group ${className}`}>{children}</div>;
}

export function ToolbarSearchInput({ value, placeholder, ariaLabel, onChange }: ToolbarSearchInputProps) {
  return (
    <input
      type="text"
      aria-label={ariaLabel}
      placeholder={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="search-input"
    />
  );
}
