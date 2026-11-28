import type { ReactNode } from "react";

export type IconName =
  | "alert"
  | "archive"
  | "check"
  | "chevron-down"
  | "chevron-right"
  | "chevron-up"
  | "clock"
  | "command"
  | "copy"
  | "download"
  | "edit"
  | "eye"
  | "eye-off"
  | "file"
  | "files"
  | "filter"
  | "folder"
  | "info"
  | "key"
  | "link"
  | "lock"
  | "plus"
  | "refresh"
  | "retry"
  | "search"
  | "server"
  | "settings"
  | "share"
  | "shield"
  | "sort"
  | "tasks"
  | "trash"
  | "upload"
  | "user"
  | "x";

const drawings: Record<IconName, ReactNode> = {
  archive: <><path d="M4 8h16v12H4ZM3 4h18v4H3ZM10 12h4" /></>,
  alert: <><path d="M10.3 4.1 2.8 17a2 2 0 0 0 1.7 3h15a2 2 0 0 0 1.7-3L13.7 4.1a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4M12 17h.01" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  "chevron-down": <path d="m6 9 6 6 6-6" />,
  "chevron-right": <path d="m9 6 6 6-6 6" />,
  "chevron-up": <path d="m6 15 6-6 6 6" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  command: <path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3Z" />,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>,
  download: <path d="M12 4v16m-6-6 6 6 6-6" />,
  edit: <path d="m14 5 5 5M4 20l4-1L19 8a2.8 2.8 0 0 0-4-4L4 15Z" />,
  eye: <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" /><circle cx="12" cy="12" r="3" /></>,
  "eye-off": <path d="m3 3 18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.5M6.2 6.2C3.4 8.2 2 12 2 12s3.5 7 10 7a10 10 0 0 0 4-.8M10 10a3 3 0 0 0 4 4" />,
  file: <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z" /><path d="M14 3v6h6M8 14h8M8 17h5" /></>,
  files: <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />,
  filter: <path d="M3 5h18l-7 8v6l-4 2v-8Z" />,
  folder: <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>,
  key: <><circle cx="8" cy="15" r="4" /><path d="m11 12 9-9M15 8l3 3M17 6l2 2" /></>,
  link: <path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.2 1.2M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.2-1.2" />,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  refresh: <path d="M20 7v5h-5M4 17v-5h5M6.1 8a7 7 0 0 1 11.8-1L20 12M4 12l2.1 5a7 7 0 0 0 11.8-1" />,
  retry: <path d="M4 10a8 8 0 1 1 2 7M4 4v6h6" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>,
  server: <><rect x="3" y="4" width="18" height="6" rx="2" /><rect x="3" y="14" width="18" height="6" rx="2" /><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6" /></>,
  settings: <path d="M4 6h16M4 12h16M4 18h16M8 4v4M15 10v4M11 16v4" />,
  share: <><circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><path d="m8.6 10.5 6.8-4M8.6 13.5l6.8 4" /></>,
  shield: <><path d="M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6Z" /><path d="m9 12 2 2 4-5" /></>,
  sort: <path d="m8 7 4-4 4 4M12 3v18m-4-4 4 4 4-4" />,
  tasks: <path d="M12 3v12m-4-4 4 4 4-4M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />,
  trash: <path d="M4 7h16M9 7V4h6v3M6 7l1 14h10l1-14M10 11v6M14 11v6" />,
  upload: <path d="M12 16V4m-4 4 4-4 4 4M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4" />,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
  x: <path d="m6 6 12 12M18 6 6 18" />,
};

type IconProps = {
  name: IconName;
  className?: string;
};

export function IconDefinitions() {
  return (
    <svg className="svg-defs" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        {Object.entries(drawings).map(([name, drawing]) => (
          <symbol key={name} id={`icon-${name}`} viewBox="0 0 24 24">
            {drawing}
          </symbol>
        ))}
      </defs>
    </svg>
  );
}

export function Icon({ name, className = "" }: IconProps) {
  return (
    <svg
      className={`icon${className ? ` ${className}` : ""}`}
      data-icon={name}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.45"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <use href={`#icon-${name}`} />
    </svg>
  );
}

export type SortDirection = "asc" | "desc" | "none";

export function SortIcon({ direction }: { direction: SortDirection }) {
  const name = direction === "asc" ? "chevron-up" : direction === "desc" ? "chevron-down" : "sort";
  return <Icon name={name} className="sort-icon icon-sm" />;
}
