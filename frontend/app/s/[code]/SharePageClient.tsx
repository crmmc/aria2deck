"use client";
import { useEffect, useRef, useState } from "react";
import { ShareInfo } from "@/types";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";

type DirItem = { name: string; is_dir: boolean; size: number; path: string };

export default function SharePageClient() {
  const [code, setCode] = useState("");
  const [shareInfo, setShareInfo] = useState<ShareInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [currentPath, setCurrentPath] = useState("");
  const [dirItems, setDirItems] = useState<DirItem[]>([]);
  const [loadingDir, setLoadingDir] = useState(false);
  const [dirError, setDirError] = useState("");
  const [siteTitle, setSiteTitle] = useState('aria2 控制器');
  const mountedRef = useRef(true);
  const downloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadDirectory = async (shareCode: string, token: string, path: string) => {
    if (!mountedRef.current) return;
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
    const parts = window.location.pathname.split("/");
    const idx = parts.indexOf("s");
    const urlCode = idx >= 0 && parts.length > idx + 1 ? parts[idx + 1] : "";
    if (!urlCode || urlCode === "_") {
      setError("无效的分享链接");
      setLoading(false);
      return;
    }
    setCode(urlCode);
    api
      .getSiteInfo()
      .then((info) => {
        if (!mountedRef.current) return;
        setSiteTitle(info.site_title);
      })
      .catch((err: unknown) => {
        console.warn("加载站点标题失败", err);
      });
    api.getShareInfo(urlCode)
      .then((info) => {
        if (!mountedRef.current) return;
        setShareInfo(info);
        if (!info.is_expired && !info.is_exhausted) {
          // document.title 由下方 useEffect 统一处理
        }
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
  // 当 siteTitle 或 shareInfo 变化时更新页面标题
  useEffect(() => {
    if (shareInfo && !shareInfo.is_expired && !shareInfo.is_exhausted) {
      document.title = `${shareInfo.file_name} - ${siteTitle}`;
    }
  }, [siteTitle, shareInfo]);

  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setPasswordError("");
    try {
      const res = await api.accessShare(code, password);
      if (!mountedRef.current) return;
      setAccessToken(res.access_token);
      if (shareInfo?.is_directory) loadDirectory(code, res.access_token, "");
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setPasswordError(err instanceof Error ? err.message : "密码错误");
    }
  };

  const handleDownload = () => {
    setDownloading(true);
    if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    downloadTimerRef.current = setTimeout(() => setDownloading(false), 2000);
    window.open(api.shareDownloadUrl(code, accessToken || undefined), "_blank");
  };

  const handleItemDownload = (itemPath: string) => {
    window.open(api.shareDownloadUrl(code, accessToken || undefined, itemPath), "_blank");
  };

  const handleDirClick = (itemPath: string) => loadDirectory(code, accessToken, itemPath);

  const handleGoBack = () => {
    if (!currentPath) return;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDirectory(code, accessToken, parts.join("/"));
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


  if (shareInfo.has_password && !accessToken) {
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
          <button
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
                  <button onClick={handleGoBack} className="button secondary" style={{ padding: "6px 12px", fontSize: 13 }}>
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
                          onClick={item.is_dir ? () => handleDirClick(item.path) : undefined}
                          onKeyDown={item.is_dir ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleDirClick(item.path); } } : undefined}
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
                    ))}
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
