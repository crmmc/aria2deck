"use client";
import { useEffect, useState } from "react";
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
  const loadDirectory = async (shareCode: string, token: string, path: string) => {
    setLoadingDir(true);
    setDirError("");
    try {
      const items = await api.browseShare(shareCode, token || undefined, path || undefined);
      setDirItems(items);
      setCurrentPath(path);
    } catch (err: unknown) {
      setDirError(err instanceof Error ? err.message : "加载目录失败");
    } finally {
      setLoadingDir(false);
    }
  };
  useEffect(() => {
    // Extract share code from URL: /s/{code}
    const parts = window.location.pathname.split("/");
    const idx = parts.indexOf("s");
    const urlCode = idx >= 0 && parts.length > idx + 1 ? parts[idx + 1] : "";
    if (!urlCode || urlCode === "_") {
      setError("无效的分享链接");
      setLoading(false);
      return;
    }
    setCode(urlCode);
    let mounted = true;
    api.getShareInfo(urlCode)
      .then((info) => {
        if (!mounted) return;
        setShareInfo(info);
        document.title = `分享 - ${info.file_name}`;
        if (info.is_directory && !info.has_password) {
          loadDirectory(urlCode, "", "");
        }
      })
      .catch((err: unknown) => {
        if (!mounted) return;
        setError(err instanceof Error ? err.message : "获取分享信息失败");
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, []);
  const handlePasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setPasswordError("");
    try {
      const res = await api.accessShare(code, password);
      setAccessToken(res.access_token);
      if (shareInfo?.is_directory) loadDirectory(code, res.access_token, "");
    } catch (err: unknown) {
      setPasswordError(err instanceof Error ? err.message : "密码错误");
    }
  };
  const handleDownload = () => {
    setDownloading(true);
    setTimeout(() => setDownloading(false), 2000);
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
  const S = {
    wrap: { display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", backgroundColor: "#0a0a0a", color: "#e5e5e5", fontFamily: "system-ui, sans-serif", padding: 20, boxSizing: "border-box" } as React.CSSProperties,
    card: { backgroundColor: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 8, padding: 32, width: "100%", maxWidth: 480, boxShadow: "0 4px 24px -4px rgba(0,0,0,0.5)" } as React.CSSProperties,
    title: { margin: "0 0 16px", fontSize: 20, fontWeight: 600, wordBreak: "break-all" as const, lineHeight: 1.4 },
    muted: { color: "#a3a3a3", fontSize: 14 },
    input: { width: "100%", padding: "10px 12px", backgroundColor: "rgba(0,0,0,0.2)", border: "1px solid rgba(255,255,255,0.2)", borderRadius: 6, color: "#e5e5e5", fontSize: 14, marginBottom: 16, boxSizing: "border-box" as const, outline: "none" } as React.CSSProperties,
    btn: { width: "100%", padding: "10px 16px", backgroundColor: "#3b82f6", color: "white", border: "none", borderRadius: 6, fontSize: 14, fontWeight: 500, cursor: "pointer" } as React.CSSProperties,
    err: { color: "#ef4444", fontSize: 14, marginBottom: 16, backgroundColor: "rgba(239,68,68,0.1)", padding: "8px 12px", borderRadius: 6, border: "1px solid rgba(239,68,68,0.2)" } as React.CSSProperties,
    list: { listStyle: "none", padding: 0, margin: 0, maxHeight: 360, overflowY: "auto" as const, border: "1px solid rgba(255,255,255,0.1)", borderRadius: 6, backgroundColor: "rgba(0,0,0,0.2)" } as React.CSSProperties,
    li: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: "1px solid rgba(255,255,255,0.05)" } as React.CSSProperties,
  };
  const msgCard = { ...S.card, textAlign: "center" as const, padding: "48px 32px" };
  if (loading) return <div style={S.wrap}><div style={msgCard}><div style={S.muted}>加载中...</div></div></div>;
  if (error) return <div style={S.wrap}><div style={msgCard}><h2 style={{ ...S.title, color: "#ef4444" }}>访问出错</h2><div style={S.muted}>{error}</div></div></div>;
  if (!shareInfo) return null;
  if (shareInfo.is_expired) return <div style={S.wrap}><div style={msgCard}><h2 style={S.title}>该分享已失效</h2><p style={{ ...S.muted, margin: 0 }}>分享链接已过期，请联系分享者重新分享</p></div></div>;
  if (shareInfo.is_exhausted) return <div style={S.wrap}><div style={msgCard}><h2 style={S.title}>下载次数已用完</h2><p style={{ ...S.muted, margin: 0 }}>该分享的下载次数已达上限</p></div></div>;
  if (shareInfo.has_password && !accessToken) {
    return (
      <div style={S.wrap}><div style={S.card}>
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>🔒</div>
          <h2 style={S.title}>{shareInfo.file_name}</h2>
          <p style={{ ...S.muted, margin: 0 }}>该分享需要提取码才能查看</p>
        </div>
        <form onSubmit={handlePasswordSubmit}>
          <input type="password" placeholder="请输入提取码" value={password}
            onChange={(e) => setPassword(e.target.value)} style={S.input} required autoFocus />
          {passwordError && <div style={S.err}>{passwordError}</div>}
          <button type="submit" style={S.btn}>提取文件</button>
        </form>
      </div></div>
    );
  }
  return (
    <div style={S.wrap}><div style={S.card}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 24 }}>
        <div style={{ fontSize: 32, lineHeight: 1 }}>{shareInfo.is_directory ? "📁" : "📄"}</div>
        <div>
          <h2 style={{ ...S.title, margin: "0 0 4px" }}>{shareInfo.file_name}</h2>
          <p style={{ ...S.muted, margin: 0 }}>{formatBytes(shareInfo.file_size)}</p>
        </div>
      </div>
      {!shareInfo.is_directory ? (
        <button onClick={handleDownload} style={{ ...S.btn, opacity: downloading ? 0.7 : 1 }} disabled={downloading}>
          {downloading ? "准备下载..." : "下载文件"}
        </button>
      ) : (
        <div>
          <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center", backgroundColor: "rgba(0,0,0,0.2)", padding: "8px 12px", borderRadius: 6 }}>
            <span style={{ fontSize: 13, color: "#a3a3a3", fontFamily: "monospace" }}>/{currentPath || "."}</span>
            {currentPath !== "" && (
              <button onClick={handleGoBack} style={{ background: "none", border: "none", color: "#e5e5e5", cursor: "pointer", fontSize: 13 }}>
                ↵ 返回上级
              </button>
            )}
          </div>
          {dirError && <div style={{ color: "#ef4444", fontSize: 13, marginBottom: 8 }}>{dirError}</div>}
          {loadingDir ? (
            <div style={{ textAlign: "center", padding: "48px 24px", ...S.muted }}>加载目录中...</div>
          ) : (
            <ul style={S.list}>
              {dirItems.length === 0 ? (
                <li style={{ padding: "32px 16px", textAlign: "center", color: "#525252", fontSize: 14 }}>空文件夹</li>
              ) : dirItems.map((item, i) => (
                <li key={i} style={S.li}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flex: 1, overflow: "hidden", cursor: item.is_dir ? "pointer" : "default" }}
                    onClick={() => item.is_dir && handleDirClick(item.path)}>
                    <span style={{ fontSize: 16 }}>{item.is_dir ? "📁" : "📄"}</span>
                    <span style={{ color: item.is_dir ? "#e5e5e5" : "#a3a3a3", fontWeight: item.is_dir ? 500 : 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.name}</span>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                    {!item.is_dir && <span style={{ fontSize: 12, color: "#737373", minWidth: 60, textAlign: "right" }}>{formatBytes(item.size)}</span>}
                    {!item.is_dir && (
                      <button onClick={() => handleItemDownload(item.path)} style={{ background: "none", border: "none", color: "#3b82f6", cursor: "pointer", padding: "4px 8px" }} title="下载">⬇</button>
                    )}
                    {item.is_dir && <span style={{ color: "#525252", fontSize: 12 }}>❯</span>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div></div>
  );
}