import type {
  DirectoryBreadcrumbResponse,
  FileEntryItemResponse,
  FileItemResponse,
  FileSearchHitResponse,
} from "@/shared/api/types";
import { formatBytes, formatDateTime } from "@/shared/lib/format";

/** 桌面表格 + 移动卡片双布局：根列表与目录浏览共用条目渲染。 */

function KindBadge({ kind }: { kind: FileEntryItemResponse["kind"] }) {
  return <span className={`badge ${kind === "directory" ? "tone-info" : "tone-ok"}`}>
    {kind === "directory" ? "文件夹" : "文件"}
  </span>;
}

export function RootFileTable({
  files,
  selected,
  highlightId,
  onToggle,
  onToggleAll,
  onOpen,
  onDownload,
  onRename,
  onShare,
  onDelete,
}: {
  files: FileItemResponse[];
  selected: Set<number>;
  highlightId: number | null;
  onToggle: (id: number) => void;
  onToggleAll: (checked: boolean) => void;
  onOpen: (file: FileItemResponse) => void;
  onDownload: (file: FileItemResponse) => void;
  onRename: (file: FileItemResponse) => void;
  onShare: (file: FileItemResponse) => void;
  onDelete: (file: FileItemResponse) => void;
}) {
  const allSelected = files.length > 0 && files.every((file) => selected.has(file.id));
  // 行处理器每行只建一份：桌面表格与移动卡片共用，行为永远一致。
  const renderRows = files.map((file) => {
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
            <th scope="col">名称</th>
            <th scope="col">类型</th>
            <th scope="col">大小</th>
            <th scope="col">入库时间</th>
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
  highlightName,
  onNavigate,
  onDownload,
}: {
  entries: FileEntryItemResponse[];
  highlightName: string | null;
  onNavigate: (entry: FileEntryItemResponse) => void;
  onDownload: (entry: FileEntryItemResponse) => void;
}) {
  const rows = entries.map((entry) => {
    const navigate = () => onNavigate(entry);
    const download = () => onDownload(entry);
    const highlighted = highlightName === entry.name;
    return { entry, navigate, download, highlighted };
  });
  return (
    <>
      <table className="data-table files-table desktop-only" aria-label="目录内容">
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">类型</th>
            <th scope="col">大小</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ entry, navigate, download, highlighted }) => (
            <tr key={entry.path} className={highlighted ? "row-highlight" : undefined}>
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
        {rows.map(({ entry, navigate, download, highlighted }) => (
          <li key={entry.path} className={`file-card${highlighted ? " row-highlight" : ""}`}>
            {entry.kind === "directory" ? (
              <button type="button" className="link-button file-card-name" onClick={navigate}>
                {entry.name}
              </button>
            ) : (
              <span className="file-card-name" title={entry.path}>
                {entry.name}
              </span>
            )}
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
