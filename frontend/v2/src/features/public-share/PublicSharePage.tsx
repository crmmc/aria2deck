"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { errorMessage } from "@/shared/api/client";
import { publicShareDownloadUrl } from "@/shared/api/urls";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { triggerFormDownload } from "@/shared/lib/download";
import { useSiteTitle } from "@/shared/lib/siteTitle";

import { BROWSE_PAGE_SIZE, browseShare, fetchShareInfo, verifyShare } from "./api";
import {
  goneCardFor,
  toShareEntryView,
  toShareInfoView,
  type ShareEntryView,
  type ShareGoneCard,
  type ShareInfoView,
} from "./model";
import { ShareDirectoryView } from "./components/ShareDirectoryView";
import { ShareDownloadActions } from "./components/ShareDownloadActions";
import { ShareHeader } from "./components/ShareHeader";
import { SharePasswordForm } from "./components/SharePasswordForm";

export function PublicSharePage() {
  const { code = "" } = useParams<{ code: string }>();
  const siteTitle = useSiteTitle();
  const [shareInfo, setShareInfo] = useState<ShareInfoView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // 410 失效分支：V2 用错误码表达 归档参考实现的 is_expired/is_exhausted 标志。
  const [goneCard, setGoneCard] = useState<ShareGoneCard | null>(null);
  // 访问令牌只存组件内存：不写 URL、localStorage 或控制台。
  const [accessToken, setAccessToken] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [currentPath, setCurrentPath] = useState("");
  const [dirItems, setDirItems] = useState<ShareEntryView[]>([]);
  const [dirPage, setDirPage] = useState(1);
  const [dirTotal, setDirTotal] = useState(0);
  const [loadingDir, setLoadingDir] = useState(false);
  const [dirError, setDirError] = useState("");
  const browseRequestIdRef = useRef(0);
  const codeRef = useRef("");
  const mountedRef = useRef(true);
  const downloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // requestId 守卫丢弃过期响应：并发导航/翻页时旧响应不得覆盖新数据
  const loadDirectory = useCallback(
    async (shareCode: string, token: string, path: string, page: number) => {
      const requestId = ++browseRequestIdRef.current;
      setLoadingDir(true);
      setDirError("");
      try {
        const result = await browseShare(shareCode, token, path, page);
        if (!mountedRef.current || requestId !== browseRequestIdRef.current) return;
        setDirItems(result.items.map(toShareEntryView));
        setDirTotal(result.total);
        setDirPage(page);
        setCurrentPath(path);
      } catch (err: unknown) {
        if (!mountedRef.current || requestId !== browseRequestIdRef.current) return;
        setDirError(errorMessage(err, "加载目录失败"));
      } finally {
        if (mountedRef.current && requestId === browseRequestIdRef.current) {
          setLoadingDir(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    mountedRef.current = true;
    const urlCode = code;
    // 分享码变化时清掉上一份分享的展示状态，避免残留内容与旧令牌串页。
    setShareInfo(null);
    setGoneCard(null);
    setError("");
    setAccessToken("");
    setPasswordError("");
    setDirItems([]);
    setDirTotal(0);
    setCurrentPath("");
    // 若 URL 带 password 参数，只预填密码框，仍由用户手动提交（归档参考语义，C19）。
    setPassword(new URLSearchParams(window.location.search).get("password") ?? "");
    if (!urlCode || urlCode === "_") {
      setError("无效的分享链接");
      setLoading(false);
      return;
    }
    setLoading(true);
    codeRef.current = urlCode;
    fetchShareInfo(urlCode)
      .then((info) => {
        if (!mountedRef.current) return;
        const view = toShareInfoView(info);
        setShareInfo(view);
        if (view.is_directory && !view.has_password) {
          void loadDirectory(urlCode, "", "", 1);
        }
      })
      .catch((err: unknown) => {
        if (!mountedRef.current) return;
        const card = goneCardFor(err);
        if (card !== null) {
          setGoneCard(card);
          return;
        }
        setError(errorMessage(err, "获取分享信息失败"));
      })
      .finally(() => {
        if (mountedRef.current) setLoading(false);
      });
    return () => {
      mountedRef.current = false;
      if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    };
  }, [code, loadDirectory]);

  // 标签页标题「文件名 - 站点标题」：站点标题晚到时随 store 更新重算。
  useEffect(() => {
    if (shareInfo === null) return;
    document.title = `${shareInfo.file_name} - ${siteTitle}`;
  }, [shareInfo, siteTitle]);

  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setPasswordError("");
    try {
      const res = await verifyShare(codeRef.current, password);
      if (!mountedRef.current) return;
      setAccessToken(res.access_token);
      // 验密成功后才摘掉地址栏里的旧密码参数；token 始终只留在内存。
      const url = new URL(window.location.href);
      if (url.searchParams.has("password")) {
        url.searchParams.delete("password");
        window.history.replaceState(null, "", url);
      }
      if (shareInfo?.is_directory) {
        void loadDirectory(codeRef.current, res.access_token, "", 1);
      }
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setPasswordError(errorMessage(err, "密码错误"));
    }
  };

  const handleDownload = () => {
    setDownloading(true);
    if (downloadTimerRef.current) clearTimeout(downloadTimerRef.current);
    downloadTimerRef.current = setTimeout(() => setDownloading(false), 2000);
    triggerFormDownload(publicShareDownloadUrl(codeRef.current), {
      token: accessToken || undefined,
    });
  };

  const handleItemDownload = (itemPath: string) => {
    triggerFormDownload(publicShareDownloadUrl(codeRef.current), {
      token: accessToken || undefined,
      path: itemPath,
    });
  };

  // 进入子目录/返回上级在事件处理器内同步重置页码，只发一笔「第 1 页」请求
  const handleDirClick = (itemPath: string) => {
    void loadDirectory(codeRef.current, accessToken, itemPath, 1);
  };

  const handleGoBack = () => {
    if (!currentPath) return;
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    void loadDirectory(codeRef.current, accessToken, parts.join("/"), 1);
  };

  const handleDirPageChange = (page: number) => {
    void loadDirectory(codeRef.current, accessToken, currentPath, page);
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

  if (goneCard !== null) {
    return (
      <div className="fixed inset-0 flex-center p-4">
        <div className="glass-frame animate-in max-w-400 w-full text-center py-12">
          <div className="text-4xl mb-4">{goneCard.icon}</div>
          <h2 className="text-lg mb-2">{goneCard.title}</h2>
          <p className="muted">{goneCard.hint}</p>
        </div>
      </div>
    );
  }

  if (!shareInfo) return null;

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
