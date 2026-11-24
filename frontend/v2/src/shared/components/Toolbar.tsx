import { useRef, type ReactNode } from "react";
import { Icon } from "./Icon";

type ToolbarShellProps = {
  children: ReactNode;
  className?: string;
};

type ToolbarGroupProps = {
  children: ReactNode;
  className: string;
};

type ToolbarSearchInputProps = {
  id?: string;
  value: string;
  placeholder: string;
  ariaLabel: string;
  clearAriaLabel?: string;
  onChange: (value: string) => void;
};

export function ToolbarShell({ children, className = "" }: ToolbarShellProps) {
  return (
    <div className={`card toolbar filter-toolbar inline-filter-toolbar${className ? ` ${className}` : ""}`}>
      {children}
    </div>
  );
}

export function ToolbarGroup({ children, className }: ToolbarGroupProps) {
  return <div className={`filter-group ${className}`}>{children}</div>;
}

export function ToolbarSearchInput({
  id,
  value,
  placeholder,
  ariaLabel,
  clearAriaLabel,
  onChange,
}: ToolbarSearchInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div className="search-box search-input-group">
      <Icon name="search" />
      <input
        ref={inputRef}
        id={id}
        type="search"
        aria-label={ariaLabel}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="input search-input toolbar-search-input"
      />
      {clearAriaLabel && (
        <button
          type="button"
          className="icon-btn search-clear"
          aria-label={clearAriaLabel}
          hidden={value === ""}
          onClick={() => {
            onChange("");
            inputRef.current?.focus();
          }}
        >
          <Icon name="x" className="icon-sm" />
        </button>
      )}
    </div>
  );
}
