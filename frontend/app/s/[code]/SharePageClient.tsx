"use client";
import { useEffect, useRef, useState } from "react";
import { ShareInfo } from "@/types";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";

type DirItem = { name: string; is_dir: boolean; size: number; path: string };
const DEFAULT_SITE_TITLE = "aria2 控制器";

export default function SharePageClient() {
  const [shareInfo, setShareInfo] = useState<ShareInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hasAccess, setHasAccess] = useState(false);
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [currentPath, setCurrentPath] = useState("");
  const [dirItems, setDirItems] = useState<DirItem[]>([]);
  const [loadingDir, setLoadingDir] = useState(false);
  const [dirError, setDirError] = useState("");
  const mountedRef = useRef(true);
  const shareCodeRef = useRef("");
  const accessTokenRef = useRef("");
  const shareInfoRef = useRef<ShareInfo | null>(null);
  const siteTitleRef = useRef(DEFAULT_SITE_TITLE);
  const downloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadDirectory = async (shareCode: string, token: string, path: string) => {
    if (!mountedRef.current) return;
    if (!shareCode) return;
    setLoadingDir(true);
    setDirError("");
    try {
      const items = await api.browseShare(shareCode, token || undefined, path || undefined);
      if (!mountedRef.current) return;
      setDirItems(items);
      setCurrentPath(path);
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setDirError(err instanceof Error ? err.message : "加载目录失败");
    } finally {
      if (mountedRef.current) setLoadingDir(false);
    }
  };

  useEffect(() => {
    mountedRef.current = true;
    const updateDocumentTitle = (
      info = shareInfoRef.current,
      title = siteTitleRef.current
    ) => {
      if (info && !info.is_expired && !info.is_exhausted) {
        document.title = `${info.file_name} - ${title}`;
      }
    };
    const parts = window.location.pathname.split("/");
    const idx = parts.indexOf("s");
    const urlCode = idx >= 0 && parts.length > idx + 1 ? parts[idx + 1] : "";
    if (!urlCode || urlCode === "_") {
      setError("无效的分享链接");
      setLoading(false);
      return;
    }
    shareCodeRef.current = urlCode;
    api
      .getSiteInfo()
      .then((info) => {
        if (!mountedRef.current) return;
        siteTitleRef.current = info.site_title;
        updateDocumentTitle();
      })
      .catch((err: unknown) => {
        console.warn("加载站点标题失败", err);
      });
    api.getShareInfo(urlCode)
      .then((info) => {
        if (!mountedRef.current) return;
        shareInfoRef.current = info;
        setShareInfo(info);
        updateDocumentTitle(info);
        if (info.is_directory && !info.has_password) {
          loadDirectory(urlCode, "", "");
        }
      })
      .catch((err: unknown) => {
        if (!mountedRef.current) return;
        setError(err instanceof Error ? err.message : "获取分享信息失败");
      })
      .finally(() => {
        if (mountedRef.current) setLoading(false);
      });
    return () => {
      mountedRef.current = false;
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    };
  }, []);

  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    const shareCode = shareCodeRef.current;
    if (!shareCode) return;
    if (!mountedRef.current) return;
    setPasswordError("");
    try {
      const res = await api.accessShare(shareCode, password);
      if (!mountedRef.current) return;
      accessTokenRef.current = res.access_token;
      setHasAccess(true);
      if (shareInfo?.is_directory) loadDirectory(shareCode, res.access_token, "");
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setPasswordError(err instanceof Error ? err.message : "密码错误");
    }
  };

  const handleDownload = () => {
    const shareCode = shareCodeRef.current;
    if (!shareCode) return;
    setDownloading(true);
    if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    downloadTimerRef.current = setTimeout(() => setDownloading(false), 2000);
    window.open(api.shareDownloadUrl(shareCode, accessTokenRef.current || undefined), "_blank");
  };

  const handleItemDownload = (itemPath: string) => {
    const shareCode = shareCodeRef.current;
    if (!shareCode) return;
    window.open(
      api.shareDownloadUrl(shareCode, accessTokenRef.current || undefined, itemPath),
      "_blank"
    );
  };

  const handleDirClick = (itemPath: string) => {
    loadDirectory(shareCodeRef.current, accessTokenRef.current, itemPath);
  };

  const handleGoBack = () => {
    if (!currentPath) return;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDirectory(shareCodeRef.current, accessTokenRef.current, parts.join("/"));
  };


  if (loading) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full text-center py-12">
          <p className="muted">加载中...</p>
        </div>
      </div>
    );
  }


  if (error) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full text-center py-12">
          <div className="text-4xl mb-4">😕</div>
          <h2 className="text-lg mb-2" style={{ color: "var(--danger)" }}>访问出错</h2>
          <p className="muted">{error}</p>
        </div>
      </div>
    );
  }

  if (!shareInfo) return null;


  if (shareInfo.is_expired) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full text-center py-12">
          <div className="text-4xl mb-4">⏰</div>
          <h2 className="text-lg mb-2">该分享已失效</h2>
          <p className="muted">分享链接已过期，请联系分享者重新分享</p>
        </div>
      </div>
    );
  }


  if (shareInfo.is_exhausted) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full text-center py-12">
          <div className="text-4xl mb-4">📊</div>
          <h2 className="text-lg mb-2">下载次数已用完</h2>
          <p className="muted">该分享的下载次数已达上限</p>
        </div>
      </div>
    );
  }


  if (shareInfo.has_password && !hasAccess) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full">
          <div className="text-center mb-7">
            <div className="text-4xl mb-4">🔒</div>
            <h2 className="text-lg mb-1" style={{ wordBreak: "break-all" }}>{shareInfo.file_name}</h2>
            <p className="muted">该分享需要提取码才能查看</p>
          </div>
          <form onSubmit={handlePasswordSubmit}>
            <div className="mb-4">
              <input
                type="password"
                aria-label="分享提取码"
                className="input"
                placeholder="请输入提取码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                ref={(el) => el?.focus()}
              />
            </div>
            {passwordError && (
              <div className="alert alert-danger text-center mb-4">{passwordError}</div>
            )}
            <button type="submit" className="button w-full">提取文件</button>
          </form>
        </div>
      </div>
    );
  }


  return (
    <div className="fixed inset-0 flex-center p-4">
      <div className="glass-frame animate-in w-full" style={{ maxWidth: 520 }}>

        <div className="row mb-6">
          <div className="text-3xl">{shareInfo.is_directory ? "📁" : "📄"}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 className="text-lg mb-1" style={{ wordBreak: "break-all", margin: 0 }}>
              {shareInfo.file_name}
            </h2>
            <p className="muted">{formatBytes(shareInfo.file_size)}</p>
          </div>
        </div>


        {!shareInfo.is_directory && (
          <button type="button"
            onClick={handleDownload}
            className="button w-full"
            disabled={downloading}
            style={{ opacity: downloading ? 0.7 : 1 }}
          >
            {downloading ? "准备下载..." : "下载文件"}
          </button>
        )}


        {shareInfo.is_directory && (
          <div>

            <div className="card mb-4" style={{ padding: "10px 16px" }}>
              <div className="space-between">
                <code className="muted" style={{ fontSize: 13 }}>/{currentPath || "."}</code>
                {currentPath !== "" && (
                  <button type="button" onClick={handleGoBack} className="button secondary" style={{ padding: "6px 12px", fontSize: 13 }}>
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
                    {dirItems.map((item, i) => {
                      const interactiveProps = item.is_dir
                        ? {
                            role: "button",
                            tabIndex: 0,
                            onClick: () => handleDirClick(item.path),
                            onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                handleDirClick(item.path);
                              }
                            },
                            "aria-label": `打开文件夹 ${item.name}`,
                          }
                        : {};

                      return (
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
                          {...interactiveProps}
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
                            <button type="button"
                              onClick={() => handleItemDownload(item.path)}
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
                      );
                    })}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
