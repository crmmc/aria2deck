import { useEffect, useRef, useState } from "react";

import { useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import { Modal } from "@/shared/components/ui";
import { formatBytes } from "@/shared/lib/format";
import type {
  PackPrecomputeResponse,
  PackViewResponse,
  ShareItemResponse,
} from "@/shared/api/types";
import { shareCopyText } from "@/features/shares/api";
import { createPack, fetchPack } from "./api";

/** 改名弹窗：409 同名等错误内联展示。 */
export function RenameDialog({
  initialName,
  onClose,
  onConfirm,
}: {
  initialName: string;
  onClose: () => void;
  onConfirm: (name: string) => Promise<void>;
}) {
  const [name, setName] = useState(initialName);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = name.trim();
    if (trimmed === "" || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(trimmed);
      onClose();
    } catch (caught) {
      setError(errorMessage(caught, "改名失败"));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="重命名"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={name.trim() === "" || submitting}
            onClick={() => void submit()}
          >
            {submitting ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      {error !== null && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <label className="form-field">
        <span>新名称（1~200 字符）</span>
        <input
          type="text"
          value={name}
          maxLength={200}
          onChange={(event) => setName(event.target.value)}
          aria-label="新名称"
        />
      </label>
    </Modal>
  );
}

const EXPIRE_OPTIONS: { value: number | null; label: string }[] = [
  { value: null, label: "永久有效" },
  { value: 24 * 60 * 60, label: "1 天" },
  { value: 7 * 24 * 60 * 60, label: "7 天" },
  { value: 30 * 24 * 60 * 60, label: "30 天" },
];

/** 创建分享弹窗：有效期 / 次数 / 密码；创建成功展示链接并可复制。 */
export function ShareCreateDialog({
  fileName,
  onClose,
  onCreate,
}: {
  fileName: string;
  onClose: () => void;
  onCreate: (
    options: { expires_in_seconds?: number | null; max_downloads?: number | null; password?: string | null },
  ) => Promise<ShareItemResponse>;
}) {
  const { push } = useToast();
  const [expires, setExpires] = useState<number | null>(null);
  const [useMaxDownloads, setUseMaxDownloads] = useState(false);
  const [maxDownloads, setMaxDownloads] = useState("10");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ShareItemResponse | null>(null);

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await onCreate({
        expires_in_seconds: expires,
        max_downloads: useMaxDownloads ? Number(maxDownloads) || null : null,
        password: password.trim() === "" ? null : password.trim(),
      });
      setCreated(result);
      push("success", "分享已创建");
    } catch (caught) {
      setError(errorMessage(caught, "创建分享失败"));
    } finally {
      setSubmitting(false);
    }
  };

  // 与分享管理页同一复制格式（WEB-05：有密码含密码行；创建时已知真实密码）。
  if (created !== null) {
    const secret = password.trim() === "" ? null : password.trim();
    const copyLink = async () => {
      try {
        await navigator.clipboard.writeText(shareCopyText(created, secret));
        push("success", "链接已复制");
      } catch {
        push("error", "复制失败，请手动复制");
      }
    };
    return (
      <Modal
        title="分享已创建"
        onClose={onClose}
        footer={
          <>
            <button type="button" className="secondary-button" onClick={onClose}>
              关闭
            </button>
            <button type="button" className="primary-button" onClick={() => void copyLink()}>
              复制链接
            </button>
          </>
        }
      >
        <p>
          文件「{fileName}」的分享链接已创建
          {created.has_password ? "，包含提取密码" : ""}。
        </p>
        <p className="share-link-text" aria-live="polite">
          {shareCopyText(created, secret)}
        </p>
      </Modal>
    );
  }

  return (
    <Modal
      title={`分享：${fileName}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button type="button" className="primary-button" disabled={submitting} onClick={() => void submit()}>
            {submitting ? "创建中…" : "创建分享"}
          </button>
        </>
      }
    >
      {error !== null && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <label className="form-field">
        <span>有效期</span>
        <select
          value={expires === null ? "" : String(expires)}
          onChange={(event) =>
            setExpires(event.target.value === "" ? null : Number(event.target.value))
          }
          aria-label="有效期"
        >
          {EXPIRE_OPTIONS.map((option) => (
            <option key={option.label} value={option.value === null ? "" : String(option.value)}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="form-field">
        <span>限制下载次数</span>
        <span className="inline-controls">
          <input
            type="checkbox"
            checked={useMaxDownloads}
            onChange={(event) => setUseMaxDownloads(event.target.checked)}
            aria-label="限制下载次数"
          />
          <input
            type="text"
            inputMode="numeric"
            value={maxDownloads}
            disabled={!useMaxDownloads}
            maxLength={5}
            onChange={(event) => setMaxDownloads(event.target.value.replace(/\D/g, ""))}
            aria-label="最大下载次数"
          />
          <span className="hint-text">次（1~10000，不填则不限）</span>
        </span>
      </label>
      <label className="form-field">
        <span>提取密码（可选，1~100 字符）</span>
        <input
          type="text"
          value={password}
          maxLength={100}
          placeholder="留空则无需密码"
          onChange={(event) => setPassword(event.target.value)}
          aria-label="提取密码"
        />
      </label>
    </Modal>
  );
}

const PACK_POLL_MS = 1000;
const TERMINAL_STATUSES = ["done", "failed", "cancelled"];
const PACK_OUTPUT_DELETED = "pack_output_deleted";

/** 打包弹窗：预计算 → 创建 → 轮询进度直到终态。 */
export function PackDialog({
  selectedIds,
  onClose,
  onPrecompute,
  onCreated,
}: {
  selectedIds: number[];
  onClose: () => void;
  onPrecompute: (ids: number[]) => Promise<PackPrecomputeResponse>;
  onCreated: () => void;
}) {
  const { push } = useToast();
  const [precompute, setPrecompute] = useState<PackPrecomputeResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [outputName, setOutputName] = useState("");
  const [deleteSource, setDeleteSource] = useState(false);
  const [pack, setPack] = useState<PackViewResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // 打开时捕获选择：父组件重渲染（如 Toast）不重发预计算请求。
  const [ids] = useState(selectedIds);
  // 回调经 ref 传递：轮询 effect 只依赖 pack，避免父组件重渲染重置定时器。
  const pushRef = useRef(push);
  pushRef.current = push;
  const onCreatedRef = useRef(onCreated);
  onCreatedRef.current = onCreated;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    let cancelled = false;
    onPrecompute(ids)
      .then((result) => {
        if (!cancelled) setPrecompute(result);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setLoadError(errorMessage(caught, "预计算失败"));
      });
    return () => {
      cancelled = true;
    };
  }, [onPrecompute, ids]);

  // 创建后轮询进度；终态停表并通知刷新。
  useEffect(() => {
    if (pack === null || TERMINAL_STATUSES.includes(pack.status)) {
      if (pack?.status === "done") {
        pushRef.current("success", `打包完成：${pack.output?.display_name ?? ""}`);
        onCreatedRef.current();
        onCloseRef.current();
      }
      return;
    }
    const timer = window.setTimeout(() => {
      void fetchPack(pack.id)
        .then(setPack)
        .catch(() => {
          /* 轮询失败保留下次重试 */
        });
    }, PACK_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [pack]);

  const insufficient =
    precompute !== null && precompute.total_size > precompute.available_bytes;
  const outputDeleted =
    pack !== null && pack.status === "failed" && pack.error_code === PACK_OUTPUT_DELETED;

  const confirm = async () => {
    if (creating || insufficient || precompute === null) return;
    const name = outputName.trim() || `打包-${precompute.items.length}项-${Date.now()}`;
    setCreating(true);
    setCreateError(null);
    try {
      const view = await createPack(ids, name, deleteSource);
      setPack(view);
      push("info", "打包任务已创建");
    } catch (caught) {
      setCreateError(errorMessage(caught, "创建打包失败"));
    } finally {
      setCreating(false);
    }
  };

  const stepLabel: Record<string, string> = {
    validating: "校验来源",
    compressing: "压缩中",
    verifying: "验收中",
  };

  return (
    <Modal
      title="打包"
      onClose={pack === null ? onClose : () => undefined}
      footer={
        pack !== null ? undefined : (
          <>
            <button type="button" className="secondary-button" onClick={onClose}>
              取消
            </button>
            <button
              type="button"
              className="primary-button"
              disabled={precompute === null || insufficient || creating}
              onClick={() => void confirm()}
            >
              {creating ? "创建中…" : "确认打包"}
            </button>
          </>
        )
      }
    >
      {loadError !== null && (
        <div className="form-error" role="alert">
          {loadError}
        </div>
      )}
      {createError !== null && (
        <div className="form-error" role="alert">
          {createError}
        </div>
      )}
      {pack !== null ? (
        <div aria-live="polite">
          <p>
            状态：
            {outputDeleted
              ? "产物已删除"
              : pack.status === "pending"
                ? "排队中"
                : stepLabel[pack.step ?? ""] ?? pack.status}
          </p>
          <div className="progress-track" role="progressbar" aria-valuenow={pack.step_progress}>
            <div className="progress-fill" style={{ width: `${pack.step_progress}%` }} />
          </div>
          <p className="hint-text">进度 {pack.step_progress}%（页面可保持打开，也可稍后在文件列表查看结果）</p>
          {pack.status === "failed" && !outputDeleted && (
            <p className="form-error">打包失败：{pack.error_code ?? "未知错误"}</p>
          )}
          {pack.status === "cancelled" && <p className="hint-text">打包已取消</p>}
        </div>
      ) : precompute === null ? (
        loadError === null && <p className="hint-text">正在计算打包大小…</p>
      ) : (
        <>
          <p>
            已选择 {ids.length} 项 · 预估 {formatBytes(precompute.total_size)} · 可用{" "}
            {formatBytes(precompute.available_bytes)}
          </p>
          {insufficient && (
            <div className="form-error" role="alert">
              空间不足，无法创建打包任务
            </div>
          )}
          <label className="form-field">
            <span>输出文件名（可选，默认自动生成）</span>
            <input
              type="text"
              value={outputName}
              maxLength={200}
              placeholder="例如：假期照片.zip"
              onChange={(event) => setOutputName(event.target.value)}
              aria-label="输出文件名"
            />
          </label>
          <label className="inline-controls">
            <input
              type="checkbox"
              checked={deleteSource}
              onChange={(event) => setDeleteSource(event.target.checked)}
              aria-label="打包后删除源文件"
            />
            <span>打包后删除源文件</span>
          </label>
        </>
      )}
    </Modal>
  );
}
