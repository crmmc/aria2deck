import type {
  DirectoryBreadcrumbResponse,
  FileEntryItemResponse,
  FileItemResponse,
  FileSearchHitResponse,
  TaskQuotaResponse,
} from "@/shared/api/types";
import { formatBytes, formatDateTime } from "@/shared/lib/format";

/** 桌面表格 + 移动卡片双布局：根列表与目录浏览共用条目渲染。 */

export type FileSortKey = "name" | "size" | "created_at";
export type FileSort = { key: FileSortKey; order: "asc" | "desc" } | null;

function KindBadge({ kind }: { kind: FileEntryItemResponse["kind"] }) {
  return <span className={`badge ${kind === "directory" ? "tone-info" : "tone-ok"}`}>
    {kind === "directory" ? "文件夹" : "文件"}
  </span>;
}

/** F3 列排序：三态表头（默认→升→降→还原）；文件夹始终置顶。 */
export function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string;
  sortKey: FileSortKey;
  sort: FileSort;
  onSort: (key: FileSortKey) => void;
}) {
  const active = sort?.key === sortKey;
  const indicator = active ? (sort.order === "asc" ? " ↑" : " ↓") : "";
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.order === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        className="sort-button"
        aria-label={`${label}排序${active ? (sort.order === "asc" ? "（升序）" : "（降序）") : ""}`}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span aria-hidden="true">{indicator}</span>
      </button>
    </th>
  );
}

function displayNameOf(item: FileItemResponse | FileEntryItemResponse): string {
  return "display_name" in item ? item.display_name : item.name;
}

function compareValues(
  a: FileItemResponse | FileEntryItemResponse,
  b: FileItemResponse | FileEntryItemResponse,
  key: FileSortKey,
): number {
  if (key === "name") return displayNameOf(a).localeCompare(displayNameOf(b), "zh-Hans-CN");
  if (key === "size") return a.size - b.size;
  const createdA = "created_at_ms" in a ? a.created_at_ms : 0;
  const createdB = "created_at_ms" in b ? b.created_at_ms : 0;
  return createdA - createdB; // 目录条目无入库时间：回退为 0（与旧版字段回退一致）。
}

/** F3：当前页客户端排序（旧版同为客户端排序），文件夹置顶后按列排序。 */
function sortEntries<T extends FileItemResponse | FileEntryItemResponse>(
  items: T[],
  sort: FileSort,
): T[] {
  if (sort === null) return items;
  const sign = sort.order === "asc" ? 1 : -1;
  return [...items].sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
    return compareValues(a, b, sort.key) * sign;
  });
}

/** F2 空间用量卡：used 实心 + frozen 斜纹分段（旧版语义）。 */
export function SpaceUsageCard({ space }: { space: TaskQuotaResponse }) {
  const total = space.quota_bytes;
  const percent = (value: number) =>
    total > 0 ? Math.min(100, Math.round((value / total) * 100)) : 0;
  const usedPercent = percent(space.used_bytes);
  const frozenPercent = percent(space.reserved_bytes);
  const totalPercent = Math.min(100, usedPercent + frozenPercent);
  const tone = totalPercent >= 80 ? "error" : totalPercent >= 50 ? "warn" : "ok";
  return (
    <section className="section-card space-card" aria-label="空间用量">
      <div className="space-card-head">
        <h2 className="section-title">可用空间</h2>
        <p className="space-card-values">
          {formatBytes(space.available_bytes)}{" "}
          <span className="hint-text">/ {formatBytes(total)}</span>
          {space.limited && (
            <span className="hint-text" role="note">
              {" "}
              · 受磁盘限制
            </span>
          )}
        </p>
      </div>
      <div
        className="progress-track space-progress"
        role="progressbar"
        aria-label="空间用量"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={totalPercent}
      >
        <div className={`progress-fill progress-${tone}`} style={{ width: `${totalPercent}%` }} />
        {space.reserved_bytes > 0 && (
          <div
            className="progress-frozen"
            style={{ left: `${usedPercent}%`, width: `${frozenPercent}%` }}
            title="冻结（下载中）"
          />
        )}
      </div>
      {space.reserved_bytes > 0 && (
        <p className="hint-text">已冻结 {formatBytes(space.reserved_bytes)}（下载中）</p>
      )}
    </section>
  );
}

export function RootFileTable({
  files,
  selected,
  highlightId,
  sort,
  onToggle,
  onToggleAll,
  onOpen,
  onDownload,
  onRename,
  onShare,
  onDelete,
  onSort,
}: {
  files: FileItemResponse[];
  selected: Set<number>;
  highlightId: number | null;
  sort: FileSort;
  onToggle: (id: number) => void;
  onToggleAll: (checked: boolean) => void;
  onOpen: (file: FileItemResponse) => void;
  onDownload: (file: FileItemResponse) => void;
  onRename: (file: FileItemResponse) => void;
  onShare: (file: FileItemResponse) => void;
  onDelete: (file: FileItemResponse) => void;
  onSort: (key: FileSortKey) => void;
}) {
  const allSelected = files.length > 0 && files.every((file) => selected.has(file.id));
  const sorted = sortEntries(files, sort);
  // 行处理器每行只建一份：桌面表格与移动卡片共用，行为永远一致。
  const renderRows = sorted.map((file) => {
    const toggle = () => onToggle(file.id);
    const open = () => onOpen(file);
    const download = () => onDownload(file);
    const rename = () => onRename(file);
    const share = () => onShare(file);
    const remove = () => onDelete(file);
    const highlighted = highlightId === file.id;
    return { file, toggle, open, download, rename, share, remove, highlighted };
  });
  return (
    <>
      <table className="data-table files-table desktop-only" aria-label="文件列表">
        <thead>
          <tr>
            <th scope="col" className="select-col">
              <input
                type="checkbox"
                aria-label="全选文件"
                checked={allSelected}
                onChange={(event) => onToggleAll(event.target.checked)}
              />
            </th>
            <SortHeader label="名称" sortKey="name" sort={sort} onSort={onSort} />
            <th scope="col">类型</th>
            <SortHeader label="大小" sortKey="size" sort={sort} onSort={onSort} />
            <SortHeader label="入库时间" sortKey="created_at" sort={sort} onSort={onSort} />
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {renderRows.map(({ file, toggle, open, download, rename, share, remove, highlighted }) => (
            <tr
              key={file.id}
              data-file-id={file.id}
              className={highlighted ? "row-highlight" : undefined}
            >
              <td className="select-col">
                <input
                  type="checkbox"
                  aria-label={`选择 ${file.display_name}`}
                  checked={selected.has(file.id)}
                  onChange={toggle}
                />
              </td>
              <td className="name-cell">
                {file.kind === "directory" ? (
                  <button
                    type="button"
                    className="link-button"
                    onClick={open}
                    aria-label={`打开文件夹 ${file.display_name}`}
                  >
                    {file.display_name}
                  </button>
                ) : (
                  <span title={file.display_name}>{file.display_name}</span>
                )}
              </td>
              <td>
                <KindBadge kind={file.kind} />
              </td>
              <td>{formatBytes(file.size)}</td>
              <td>{formatDateTime(file.created_at_ms)}</td>
              <td className="actions-cell">
                {file.kind === "file" && (
                  <button type="button" className="link-button" onClick={download}>
                    下载
                  </button>
                )}
                <button type="button" className="link-button" onClick={rename}>
                  改名
                </button>
                <button type="button" className="link-button" onClick={share}>
                  分享
                </button>
                <button type="button" className="link-button" onClick={remove}>
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="file-card-list mobile-only" aria-label="文件卡片列表">
        {renderRows.map(({ file, toggle, open, download, rename, share, remove, highlighted }) => (
          <li
            key={file.id}
            data-file-id={file.id}
            className={`file-card${highlighted ? " row-highlight" : ""}`}
          >
            <label className="file-card-main">
              <input
                type="checkbox"
                aria-label={`选择 ${file.display_name}`}
                checked={selected.has(file.id)}
                onChange={toggle}
              />
              {file.kind === "directory" ? (
                <button type="button" className="link-button file-card-name" onClick={open}>
                  {file.display_name}
                </button>
              ) : (
                <span className="file-card-name" title={file.display_name}>
                  {file.display_name}
                </span>
              )}
            </label>
            <div className="file-card-meta">
              <KindBadge kind={file.kind} />
              <span>{formatBytes(file.size)}</span>
            </div>
            <div className="file-card-actions">
              {file.kind === "file" && (
                <button type="button" className="link-button" onClick={download}>
                  下载
                </button>
              )}
              <button type="button" className="link-button" onClick={rename}>
                改名
              </button>
              <button type="button" className="link-button" onClick={share}>
                分享
              </button>
              <button type="button" className="link-button" onClick={remove}>
                删除
              </button>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

export function EntryTable({
  entries,
  selected,
  highlightName,
  sort,
  onToggle,
  onToggleAll,
  onNavigate,
  onDownload,
  onSort,
}: {
  entries: FileEntryItemResponse[];
  selected: Set<string>;
  highlightName: string | null;
  sort: FileSort;
  onToggle: (path: string) => void;
  onToggleAll: (checked: boolean) => void;
  onNavigate: (entry: FileEntryItemResponse) => void;
  onDownload: (entry: FileEntryItemResponse) => void;
  onSort: (key: FileSortKey) => void;
}) {
  const selectable = entries.filter((entry) => entry.kind === "file");
  const allSelected = selectable.length > 0 && selectable.every((entry) => selected.has(entry.path));
  const sorted = sortEntries(entries, sort);
  const rows = sorted.map((entry) => {
    const navigate = () => onNavigate(entry);
    const download = () => onDownload(entry);
    const highlighted = highlightName === entry.name;
    const checked = selected.has(entry.path);
    return { entry, navigate, download, highlighted, checked };
  });
  return (
    <>
      <table className="data-table files-table desktop-only" aria-label="目录内容">
        <thead>
          <tr>
            <th scope="col" className="select-col">
              <input
                type="checkbox"
                aria-label="全选文件（仅文件）"
                checked={allSelected}
                disabled={selectable.length === 0}
                onChange={(event) => onToggleAll(event.target.checked)}
              />
            </th>
            <SortHeader label="名称" sortKey="name" sort={sort} onSort={onSort} />
            <th scope="col">类型</th>
            <SortHeader label="大小" sortKey="size" sort={sort} onSort={onSort} />
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ entry, navigate, download, highlighted, checked }) => (
            <tr key={entry.path} className={highlighted ? "row-highlight" : undefined}>
              <td className="select-col">
                {/* F7：文件夹条目禁选（目录走打包整体下载）。 */}
                <input
                  type="checkbox"
                  aria-label={`选择 ${entry.name}`}
                  checked={checked}
                  disabled={entry.kind === "directory"}
                  title={entry.kind === "directory" ? "文件夹不能批量下载" : undefined}
                  onChange={() => onToggle(entry.path)}
                />
              </td>
              <td className="name-cell">
                {entry.kind === "directory" ? (
                  <button
                    type="button"
                    className="link-button"
                    onClick={navigate}
                    aria-label={`进入子目录 ${entry.name}`}
                  >
                    {entry.name}
                  </button>
                ) : (
                  <span title={entry.path}>{entry.name}</span>
                )}
              </td>
              <td>
                <KindBadge kind={entry.kind} />
              </td>
              <td>{formatBytes(entry.size)}</td>
              <td className="actions-cell">
                {entry.kind === "file" && (
                  <button type="button" className="link-button" onClick={download}>
                    下载
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="file-card-list mobile-only" aria-label="目录内容卡片">
        {rows.map(({ entry, navigate, download, highlighted, checked }) => (
          <li key={entry.path} className={`file-card${highlighted ? " row-highlight" : ""}`}>
            <label className="file-card-main">
              <input
                type="checkbox"
                aria-label={`选择 ${entry.name}`}
                checked={checked}
                disabled={entry.kind === "directory"}
                onChange={() => onToggle(entry.path)}
              />
              {entry.kind === "directory" ? (
                <button type="button" className="link-button file-card-name" onClick={navigate}>
                  {entry.name}
                </button>
              ) : (
                <span className="file-card-name" title={entry.path}>
                  {entry.name}
                </span>
              )}
            </label>
            <div className="file-card-meta">
              <KindBadge kind={entry.kind} />
              <span>{formatBytes(entry.size)}</span>
              {entry.kind === "file" && (
                <button type="button" className="link-button" onClick={download}>
                  下载
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

export function Breadcrumbs({
  items,
  onNavigate,
}: {
  items: DirectoryBreadcrumbResponse[];
  onNavigate: (path: string) => void;
}) {
  return (
    <nav className="breadcrumbs" aria-label="目录路径">
      {items.map((crumb, index) => (
        <span key={crumb.path} className="breadcrumb-item">
          {index > 0 && <span aria-hidden="true">/</span>}
          <button
            type="button"
            className="link-button"
            onClick={() => onNavigate(crumb.path)}
            aria-current={index === items.length - 1 ? "page" : undefined}
          >
            {crumb.name}
          </button>
        </span>
      ))}
    </nav>
  );
}

export function SearchResults({
  hits,
  truncated,
  onLocate,
}: {
  hits: FileSearchHitResponse[];
  truncated: boolean;
  onLocate: (hit: FileSearchHitResponse) => void;
}) {
  if (hits.length === 0) {
    return <p className="hint-text">没有匹配的文件</p>;
  }
  return (
    <div className="search-results" role="listbox" aria-label="搜索结果">
      {truncated && (
        <p className="hint-text" role="status">
          结果超过 200 条，仅显示前 200 条，请细化关键词
        </p>
      )}
      <ul>
        {hits.map((hit) => (
          <li key={`${hit.user_file_id}:${hit.path}`}>
            <button
              type="button"
              role="option"
              aria-selected={false}
              className="search-hit"
              onClick={() => onLocate(hit)}
            >
              <span className="search-hit-name">{hit.path === "" ? hit.display_name : hit.path}</span>
              <span className="search-hit-meta">
                {hit.kind === "directory" ? "文件夹" : "文件"} · {formatBytes(hit.size)} ·{" "}
                {hit.path === "" ? "根目录" : `位于 ${hit.display_name}`}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
