import { formatBytes } from "@/lib/utils";

type DirItem = { name: string; is_dir: boolean; size: number; path: string };

type ShareDirectoryViewProps = {
  currentPath: string;
  dirItems: DirItem[];
  loadingDir: boolean;
  dirError: string;
  onGoBack: () => void;
  onDirClick: (path: string) => void;
  onItemDownload: (path: string) => void;
};

export function ShareDirectoryView({
  currentPath,
  dirItems,
  loadingDir,
  dirError,
  onGoBack,
  onDirClick,
  onItemDownload,
}: ShareDirectoryViewProps) {
  return (
    <div>
      <div className="card mb-4" style={{ padding: "10px 16px" }}>
        <div className="space-between">
          <code className="muted" style={{ fontSize: 13 }}>/{currentPath || "."}</code>
          {currentPath !== "" && (
            <button type="button" onClick={onGoBack} className="button secondary" style={{ padding: "6px 12px", fontSize: 13 }}>
              ↵ 返回上级
            </button>
          )}
        </div>
      </div>

      {dirError && (
        <div className="alert alert-danger mb-4">{dirError}</div>
      )}

      {loadingDir ? (
        <div className="text-center py-8">
          <p className="muted">加载目录中...</p>
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          {dirItems.length === 0 ? (
            <div className="text-center py-8">
              <p className="muted">空文件夹</p>
            </div>
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: 0, maxHeight: 360, overflowY: "auto" }}>
              {dirItems.map((item, i) => (
                <li
                  key={item.path}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "12px 16px",
                    borderBottom: i < dirItems.length - 1 ? "1px solid rgba(0,0,0,0.05)" : "none",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      flex: 1,
                      overflow: "hidden",
                      cursor: item.is_dir ? "pointer" : "default",
                    }}
                    role={item.is_dir ? "button" : undefined}
                    tabIndex={item.is_dir ? 0 : undefined}
                    onClick={item.is_dir ? () => onDirClick(item.path) : undefined}
                    onKeyDown={item.is_dir ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onDirClick(item.path); } } : undefined}
                  >
                    <span style={{ fontSize: 16 }}>{item.is_dir ? "📁" : "📄"}</span>
                    <span
                      style={{
                        fontWeight: item.is_dir ? 500 : 400,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {item.name}
                    </span>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    {!item.is_dir && (
                      <span className="muted" style={{ fontSize: 12, minWidth: 60, textAlign: "right" }}>
                        {formatBytes(item.size)}
                      </span>
                    )}
                    {!item.is_dir && (
                      <button
                        type="button"
                        onClick={() => onItemDownload(item.path)}
                        className="button secondary"
                        style={{ padding: "4px 10px", fontSize: 12 }}
                        title="下载"
                      >
                        下载
                      </button>
                    )}
                    {item.is_dir && (
                      <span className="muted" style={{ fontSize: 12 }}>❯</span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
