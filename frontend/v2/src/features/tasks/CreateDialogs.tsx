import { useState } from "react";

import { Modal } from "@/shared/components/ui";
import { errorMessage } from "@/shared/api/client";
import { formatBytes } from "@/shared/lib/format";
import type { TorrentPreviewResponse } from "@/shared/api/types";
import type { CreateOutcome } from "./api";

/** URI 创建弹窗：textarea 多行 = 批量。 */
export function UriCreateDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (text: string) => Promise<CreateOutcome | null>;
}) {
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const lineCount = text
    .split(/\r?\n/)
    .filter((line) => line.trim() !== "").length;

  const submit = async () => {
    if (lineCount === 0 || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onCreate(text);
      // 成功或部分失败均由父组件管理弹窗切换（success → 关闭 / failures → 换弹窗）。
    } catch (caught) {
      setError(errorMessage(caught, "创建失败"));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="新建下载任务"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={lineCount === 0 || submitting}
            onClick={() => void submit()}
          >
            {submitting ? "提交中…" : `添加 ${lineCount > 0 ? lineCount : ""} 任务`.trim()}
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
        <span>下载链接（每行一条，支持 HTTP(S) 与 Magnet）</span>
        <textarea
          rows={6}
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={"https://example.com/file.iso\nmagnet:?xt=urn:btih:…"}
          aria-label="下载链接列表"
        />
      </label>
    </Modal>
  );
}

/** 种子创建弹窗：选择文件 → 预览 → 勾选 → 创建。 */
export function TorrentDialog({
  preview,
  onClose,
  onCreate,
}: {
  preview: TorrentPreviewResponse;
  onClose: () => void;
  onCreate: (selected: number[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Set<number>>(
    new Set(preview.default_selection),
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = (index: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      // 成功或部分失败均由父组件管理弹窗切换（部分失败换 FailuresDialog，不能被关闭）。
      await onCreate([...selected].sort((a, b) => a - b));
    } catch (caught) {
      setError(errorMessage(caught, "创建失败"));
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={`种子：${preview.name}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className="primary-button"
            disabled={selected.size === 0 || submitting}
            onClick={() => void submit()}
          >
            {submitting ? "创建中…" : `创建（${selected.size} 个文件）`}
          </button>
        </>
      }
    >
      {error !== null && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <p>
        总大小 {formatBytes(preview.total_size)}，已选 {selected.size} / {preview.files.length} 个文件
      </p>
      <ul className="torrent-file-list">
        {preview.files.map((file) => (
          <li key={file.index}>
            <label>
              <input
                type="checkbox"
                checked={selected.has(file.index)}
                onChange={() => toggle(file.index)}
              />
              <span className="torrent-file-path" title={file.path}>
                {file.path}
              </span>
              <span className="torrent-file-size">{formatBytes(file.size)}</span>
            </label>
          </li>
        ))}
      </ul>
    </Modal>
  );
}

/** 批量结果弹窗：部分失败保留弹窗、仅重试失败项。 */
export function FailureDialog({
  failedItems,
  onRetryFailed,
  onClose,
}: {
  failedItems: { uri: string; message: string }[];
  onRetryFailed: () => void;
  onClose: () => void;
}) {
  return (
    <Modal
      title={`${failedItems.length} 条任务失败`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="secondary-button" onClick={onClose}>
            关闭
          </button>
          <button type="button" className="primary-button" onClick={onRetryFailed}>
            仅重试失败项
          </button>
        </>
      }
    >
      <ul className="failure-list">
        {failedItems.map((item) => (
          <li key={item.uri}>
            <span className="failure-uri" title={item.uri}>
              {item.uri}
            </span>
            <span className="failure-message">{item.message}</span>
          </li>
        ))}
      </ul>
    </Modal>
  );
}
