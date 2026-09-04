"use client";
import { useEffect, useRef, useState } from "react";
import { BrowseFileInfo, ShareInfo } from "@/types";
import { api } from "@/lib/api";
import { PaginationControls } from "@/components/ui/PaginationControls";
import { ShareHeader } from "./_components/ShareHeader";
import { SharePasswordForm } from "./_components/SharePasswordForm";
import { ShareDirectoryView } from "./_components/ShareDirectoryView";
import { ShareDownloadActions } from "./_components/ShareDownloadActions";

// 目录浏览分页页大小（与后端 BROWSE_DEFAULT_PAGE_SIZE 保持一致）
const BROWSE_PAGE_SIZE = 200;

export default function SharePageClient() {
  const [shareInfo, setShareInfo] = useState<ShareInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [currentPath, setCurrentPath] = useState("");
  const [dirItems, setDirItems] = useState<BrowseFileInfo[]>([]);
  const [dirPage, setDirPage] = useState(1);
  const [dirTotal, setDirTotal] = useState(0);
  const [loadingDir, setLoadingDir] = useState(false);
  const [dirError, setDirError] = useState("");
  const browseRequestIdRef = useRef(0);
  const codeRef = useRef("");
  const shareInfoRef = useRef<ShareInfo | null>(null);
  const siteTitleRef = useRef("aria2 控制器");
  const mountedRef = useRef(true);
  const downloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updateDocumentTitle = (info: ShareInfo | null) => {
    if (info && !info.is_expired && !info.is_exhausted) {
      document.title = `${info.file_name} - ${siteTitleRef.current}`;
    }
  };

  // requestId 守卫丢弃过期响应：并发导航/翻页时旧响应不得覆盖新数据
  const loadDirectory = async (shareCode: string, token: string, path: string, page: number) => {
    const requestId = ++browseRequestIdRef.current;
    setLoadingDir(true);
    setDirError("");
    try {
      const result = await api.browseShare(
        shareCode,
        token || undefined,
        path || undefined,
        page,
        BROWSE_PAGE_SIZE
      );
      if (!mountedRef.current || requestId !== browseRequestIdRef.current) return;
      setDirItems(result.items);
      setDirTotal(result.total);
      setDirPage(page);
      setCurrentPath(path);
    } catch (err: unknown) {
      if (!mountedRef.current || requestId !== browseRequestIdRef.current) return;
      setDirError(err instanceof Error ? err.message : "加载目录失败");
    } finally {
      if (mountedRef.current && requestId === browseRequestIdRef.current) {
        setLoadingDir(false);
      }
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
    codeRef.current = urlCode;
    // 若 URL 带 password 参数，自动填入密码框
    const urlPassword = new URLSearchParams(window.location.search).get("password");
    if (urlPassword) {
      setPassword(urlPassword);
    }
    api
      .getSiteInfo()
      .then((info) => {
        if (!mountedRef.current) return;
        siteTitleRef.current = info.site_title;
        updateDocumentTitle(shareInfoRef.current);
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
          loadDirectory(urlCode, "", "", 1);
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
    setPasswordError("");
    try {
      const res = await api.accessShare(codeRef.current, password);
      if (mountedRef.current) {
        setAccessToken(res.access_token);
        if (shareInfo?.is_directory) loadDirectory(codeRef.current, res.access_token, "", 1);
      }
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setPasswordError(err instanceof Error ? err.message : "密码错误");
    }
  };

  const handleDownload = () => {
    setDownloading(true);
    if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    downloadTimerRef.current = setTimeout(() => setDownloading(false), 2000);
    api.downloadShare(codeRef.current, accessToken || undefined);
  };

  const handleItemDownload = (itemPath: string) => {
    api.downloadShare(codeRef.current, accessToken || undefined, itemPath);
  };

  // 进入子目录/返回上级在事件处理器内同步重置页码，只发一笔「第 1 页」请求
  const handleDirClick = (itemPath: string) =>
    loadDirectory(codeRef.current, accessToken, itemPath, 1);

  const handleGoBack = () => {
    if (!currentPath) return;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDirectory(codeRef.current, accessToken, parts.join("/"), 1);
  };

  const handleDirPageChange = (page: number) =>
    loadDirectory(codeRef.current, accessToken, currentPath, page);

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
      <SharePasswordForm
        shareInfo={shareInfo}
        password={password}
        passwordError={passwordError}
        onPasswordChange={setPassword}
        onSubmit={handlePasswordSubmit}
      />
    );
  }

  return (
    <div className="fixed inset-0 flex-center p-4">
      <div className="glass-frame animate-in w-full" style={{ maxWidth: 520 }}>
        <ShareHeader shareInfo={shareInfo} />

        {!shareInfo.is_directory && (
          <ShareDownloadActions downloading={downloading} onDownload={handleDownload} />
        )}

        {shareInfo.is_directory && (
          <>
            <ShareDirectoryView
              currentPath={currentPath}
              dirItems={dirItems}
              loadingDir={loadingDir}
              dirError={dirError}
              onGoBack={handleGoBack}
              onDirClick={handleDirClick}
              onItemDownload={handleItemDownload}
            />
            {!loadingDir && dirTotal > BROWSE_PAGE_SIZE && (
              <PaginationControls
                currentPage={dirPage}
                pageSize={BROWSE_PAGE_SIZE}
                totalFiles={dirTotal}
                onPageChange={handleDirPageChange}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}
