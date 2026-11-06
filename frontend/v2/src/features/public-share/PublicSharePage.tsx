import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useParams } from "react-router-dom";

import { api, ApiRequestError, errorMessage } from "@/shared/api/client";
import { publicShareDownloadUrl } from "@/shared/api/urls";
import {
  EmptyState,
  LoadingState,
  Pagination,
} from "@/shared/components/ui";
import { formatBytes, formatDateTime } from "@/shared/lib/format";
import { triggerFormDownload } from "@/shared/lib/download";
import { useSiteTitle } from "@/shared/lib/siteTitle";
import type {
  DirectoryBreadcrumbResponse,
  FileEntryItemResponse,
  PublicEntryListResponse,
  PublicShareInfoResponse,
} from "@/shared/api/types";

/** WEB-10 公开分享页：独立布局、内存 token（不写 URL/localStorage）、失效态完整。 */

const ENTRY_PAGE_SIZE = 200;

interface InvalidState {
  title: string;
  hint: string;
}

const INVALID_BY_CODE: Record<string, InvalidState> = {
  share_revoked: { title: "分享已撤销", hint: "分享者已撤销该链接，请联系对方重新分享" },
  share_expired: { title: "分享已过期", hint: "该分享链接已过期，请联系分享者重新分享" },
  share_exhausted: { title: "下载次数已用完", hint: "该分享的下载次数已达上限" },
  share_unavailable: { title: "分享文件已删除", hint: "分享者已删除该文件，链接不再可用" },
};

function invalidState(error: ApiRequestError): InvalidState {
  if (error.status === 404) return { title: "分享不存在", hint: "链接无效或已被删除" };
  return INVALID_BY_CODE[error.code] ?? { title: "访问出错", hint: error.message };
}

export function PublicSharePage() {
  const { code = "" } = useParams<{ code: string }>();
  const location = useLocation();
  const siteTitle = useSiteTitle();
  const [info, setInfo] = useState<PublicShareInfoResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [invalid, setInvalid] = useState<InvalidState | null>(null);
  // 访问令牌只存组件内存（WEB-13：绝不写 URL/localStorage）。
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  // 目录浏览
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<FileEntryItemResponse[]>([]);
  const [breadcrumbs, setBreadcrumbs] = useState<DirectoryBreadcrumbResponse[]>([]);
  const [entryTotal, setEntryTotal] = useState(0);
  const [entryPage, setEntryPage] = useState(1);
  const [entryLoading, setEntryLoading] = useState(false);
  const [entryError, setEntryError] = useState<string | null>(null);
  const browseSeq = useRef(0);
  const tokenRef = useRef<string | null>(null);
  tokenRef.current = token;
  const isDirectory = info?.file_kind === "directory";

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .get<PublicShareInfoResponse>(`/api/s/${encodeURIComponent(code)}/info`)
      .then((value) => {
        if (!cancelled) setInfo(value);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setInvalid(
            caught instanceof ApiRequestError
              ? invalidState(caught)
              : { title: "访问出错", hint: "网络异常，请稍后重试" },
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [code]);

  // K3 标签页标题：文件名 - 站点标题。
  useEffect(() => {
    document.title = info === null ? siteTitle : `${info.file_name} - ${siteTitle}`;
  }, [info, siteTitle]);

  // S1+K1 双轨：旧版链接 ?password= 挂载时自动验密一次（仅内存，不回显表单）。
  // 成功后 history.replaceState 清掉地址栏参数；失败展示验密表单并内联提示。
  const legacyPasswordRef = useRef<string | null>(null);
  if (legacyPasswordRef.current === null) {
    const fromUrl = new URLSearchParams(location.search).get("password");
    legacyPasswordRef.current = fromUrl === null || fromUrl === "" ? null : fromUrl;
  }
  const autoVerifiedRef = useRef(false);
  useEffect(() => {
    const legacy = legacyPasswordRef.current;
    if (
      info === null ||
      autoVerifiedRef.current ||
      legacy === null ||
      !info.has_password ||
      token !== null
    ) {
      return;
    }
    autoVerifiedRef.current = true;
    api
      .post<{ access_token: string; expires_in: number }>(
        `/api/s/${encodeURIComponent(code)}/verify`,
        { password: legacy },
      )
      .then((response) => {
        setToken(response.access_token);
        const url = new URL(window.location.href);
        url.searchParams.delete("password");
        window.history.replaceState(null, "", url);
      })
      .catch(() => {
        setPasswordError("链接携带的密码无效，请手动输入");
      });
  }, [info, code, token]);

  const bearer = () =>
    tokenRef.current === null ? undefined : { Authorization: `Bearer ${tokenRef.current}` };

  const loadEntries = useCallback(
    async (targetPath: string, page: number) => {
      const seq = ++browseSeq.current;
      setEntryLoading(true);
      setEntryError(null);
      try {
        const response = await api.get<PublicEntryListResponse>(
          `/api/s/${encodeURIComponent(code)}/entries?${new URLSearchParams({
            path: targetPath,
            page: String(page),
            page_size: String(ENTRY_PAGE_SIZE),
          }).toString()}`,
          { headers: bearer() },
        );
        if (seq !== browseSeq.current) return; // 丢弃过期响应。
        setEntries(response.items);
        setBreadcrumbs(response.breadcrumbs);
        setEntryTotal(response.total);
        setEntryPage(page);
        setPath(targetPath);
      } catch (caught) {
        if (seq !== browseSeq.current) return;
        setEntryError(errorMessage(caught, "加载目录失败"));
      } finally {
        if (seq === browseSeq.current) setEntryLoading(false);
      }
    },
    [code],
  );

  const handleVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    if (verifying || password === "") return;
    setVerifying(true);
    setPasswordError(null);
    try {
      const response = await api.post<{ access_token: string; expires_in: number }>(
        `/api/s/${encodeURIComponent(code)}/verify`,
        { password },
      );
      setToken(response.access_token); // 仅组件内存，刷新后需重新验证。
      setPassword("");
    } catch (caught) {
      setPasswordError(errorMessage(caught, "验证失败，请重试"));
    } finally {
      setVerifying(false);
    }
  };

  // 目录浏览时机：无密码分享凭分享码直连；有密码分享等验密通过（token 进内存）。
  useEffect(() => {
    if (info === null || !isDirectory) return;
    if (info.has_password && token === null) return; // 验密表单负责获取令牌。
    void loadEntries("", 1);
  }, [info, isDirectory, token, loadEntries]);

  // 下载：POST 表单提交（有密码分享令牌放隐藏域；无密码直连省略 token；绝不进 URL）。
  const handleDownload = (targetPath?: string) => {
    const fields: { token?: string; path?: string } = {};
    if (info?.has_password === true) {
      if (tokenRef.current === null) return; // 有密码时内容视图仅在验密后出现。
      fields.token = tokenRef.current;
    }
    if (targetPath !== undefined) fields.path = targetPath;
    triggerFormDownload(publicShareDownloadUrl(code), fields);
  };

  if (loading) return <LoadingState label="正在加载分享…" />;

  if (invalid !== null) {
    return (
      <div className="public-share-page">
        <div className="public-share-card">
          <h1>{invalid.title}</h1>
          <p className="hint-text">{invalid.hint}</p>
        </div>
      </div>
    );
  }

  if (info === null) return null;

  if (info.has_password && token === null) {
    return (
      <div className="public-share-page">
        <form className="public-share-card" onSubmit={handleVerify} aria-label="提取密码验证">
          <h1>{info.file_name}</h1>
          <p className="hint-text">
            该分享需要提取密码（{formatBytes(info.file_size)}）
          </p>
          {passwordError !== null && (
            <div className="form-error" role="alert">
              {passwordError}
            </div>
          )}
          <label className="form-field">
            <span>提取密码</span>
            <input
              type="password"
              value={password}
              maxLength={100}
              autoComplete="off"
              onChange={(event) => setPassword(event.target.value)}
              aria-label="提取密码"
            />
          </label>
          <button type="submit" className="primary-button" disabled={verifying || password === ""}>
            {verifying ? "验证中…" : "验证并查看"}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="public-share-page">
      <div className="public-share-card">
        <h1 title={info.file_name}>{info.file_name}</h1>
        <p className="share-meta">
          {isDirectory ? "文件夹" : "文件"} · {formatBytes(info.file_size)} · 下载{" "}
          {info.download_count}
          {info.max_downloads === null ? " 次（不限）" : ` / ${info.max_downloads} 次`}
          {info.expires_at_ms !== null && ` · ${formatDateTime(info.expires_at_ms)} 过期`}
        </p>

        {!isDirectory && (
          <button
            type="button"
            className="primary-button"
            onClick={() => handleDownload()}
          >
            下载文件
          </button>
        )}

        {entryError !== null && (
          <p className="form-error" role="alert">
            {entryError}
          </p>
        )}

        {isDirectory && (
          <>
            <nav className="breadcrumbs" aria-label="目录路径">
              {breadcrumbs.map((crumb, index) => (
                <span key={crumb.path} className="breadcrumb-item">
                  {index > 0 && <span aria-hidden="true">/</span>}
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => void loadEntries(crumb.path, 1)}
                    aria-current={index === breadcrumbs.length - 1 ? "page" : undefined}
                  >
                    {crumb.name}
                  </button>
                </span>
              ))}
            </nav>
            {entryLoading ? (
              <LoadingState label="正在加载目录…" />
            ) : entries.length === 0 ? (
              <EmptyState title="空目录" hint="该目录下没有内容。" />
            ) : (
              <>
                <table className="data-table" aria-label="分享目录内容">
                  <thead>
                    <tr>
                      <th scope="col">名称</th>
                      <th scope="col">大小</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr key={entry.path}>
                        <td className="name-cell">
                          {entry.kind === "directory" ? (
                            <button
                              type="button"
                              className="link-button"
                              onClick={() => void loadEntries(entry.path, 1)}
                              aria-label={`进入子目录 ${entry.name}`}
                            >
                              {entry.name}
                            </button>
                          ) : (
                            <span title={entry.path}>{entry.name}</span>
                          )}
                        </td>
                        <td>{formatBytes(entry.size)}</td>
                        <td className="actions-cell">
                          {entry.kind === "file" && (
                            <button
                              type="button"
                              className="link-button"
                              onClick={() => handleDownload(entry.path)}
                            >
                              下载
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Pagination
                  page={entryPage}
                  pageSize={ENTRY_PAGE_SIZE}
                  total={entryTotal}
                  onChange={(next) => void loadEntries(path, next)}
                />
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
