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
      <div className="card mb-4 share-directory-path-card">
        <div className="space-between">
          <code className="muted share-directory-path">/{currentPath || "."}</code>
          {currentPath !== "" && (
            <button type="button" onClick={onGoBack} className="button secondary share-directory-back-button">
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
        <div className="card share-directory-list-card">
          {dirItems.length === 0 ? (
            <div className="text-center py-8">
              <p className="muted">空文件夹</p>
            </div>
          ) : (
            <ul className="share-directory-list">
              {dirItems.map((item) => (
                <li
                  key={item.path}
                  className="share-directory-row"
                >
                  {item.is_dir ? (
                    <button
                      type="button"
                      className="share-directory-item-main share-directory-dir-button"
                      onClick={() => onDirClick(item.path)}
                    >
                      <span className="share-directory-icon">📁</span>
                      <span className="share-directory-item-name share-directory-dir-name">
                        {item.name}
                      </span>
                    </button>
                  ) : (
                    <div className="share-directory-item-main">
                      <span className="share-directory-icon">📄</span>
                      <span className="share-directory-item-name">
                        {item.name}
                      </span>
                    </div>
                  )}
                  <div className="share-directory-actions">
                    {!item.is_dir && (
                      <span className="muted share-directory-size">
                        {formatBytes(item.size)}
                      </span>
                    )}
                    {!item.is_dir && (
                      <button
                        type="button"
                        onClick={() => onItemDownload(item.path)}
                        className="button secondary share-directory-download-button"
                        title="下载"
                      >
                        下载
                      </button>
                    )}
                    {item.is_dir && (
                      <span className="muted share-directory-chevron">❯</span>
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
