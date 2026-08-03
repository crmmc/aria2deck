"use client";
import { useEffect, useRef, useState } from "react";
import { ShareInfo } from "@/types";
import { api } from "@/lib/api";
import { ShareHeader } from "./_components/ShareHeader";
import { SharePasswordForm } from "./_components/SharePasswordForm";
import { ShareDirectoryView } from "./_components/ShareDirectoryView";
import { ShareDownloadActions } from "./_components/ShareDownloadActions";

type DirItem = { name: string; is_dir: boolean; size: number; path: string };

export default function SharePageClient() {
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

  const loadDirectory = async (shareCode: string, token: string, path: string) => {
    if (!mountedRef.current) return;
    setLoadingDir(true);
    setDirError("");
    try {
      const items = await api.browseShare(shareCode, token || undefined, path || undefined);
      if (mountedRef.current) {
        setDirItems(items);
        setCurrentPath(path);
      }
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
    codeRef.current = urlCode;
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
    setPasswordError("");
    try {
      const res = await api.accessShare(codeRef.current, password);
      if (mountedRef.current) {
        setAccessToken(res.access_token);
        if (shareInfo?.is_directory) loadDirectory(codeRef.current, res.access_token, "");
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

  const handleDirClick = (itemPath: string) => loadDirectory(codeRef.current, accessToken, itemPath);

  const handleGoBack = () => {
    if (!currentPath) return;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDirectory(codeRef.current, accessToken, parts.join("/"));
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
          <ShareDirectoryView
            currentPath={currentPath}
            dirItems={dirItems}
            loadingDir={loadingDir}
            dirError={dirError}
            onGoBack={handleGoBack}
            onDirClick={handleDirClick}
            onItemDownload={handleItemDownload}
          />
        )}
      </div>
    </div>
  );
}
