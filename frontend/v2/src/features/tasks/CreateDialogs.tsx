import { useMemo, useState } from "react";

import { Modal } from "@/shared/components/ui";
import { errorMessage } from "@/shared/api/client";
import { formatBytes } from "@/shared/lib/format";
import type { TorrentFilePreviewResponse, TorrentPreviewResponse } from "@/shared/api/types";
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

/* ============ 种子三阶段向导（T8）：选择文件 → 树形勾选 → 确认创建 ============ */

type TreeFile = { kind: "file"; name: string; index: number; size: number };
type TreeFolder = {
  kind: "folder";
  name: string;
  children: TreeNode[];
  fileIndexes: number[];
  size: number;
};
type TreeNode = TreeFile | TreeFolder;

function finalizeFolder(folder: TreeFolder): void {
  folder.children.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "folder" ? -1 : 1;
    return a.name.localeCompare(b.name, "zh-Hans-CN");
  });
  folder.size = 0;
  folder.fileIndexes = [];
  for (const child of folder.children) {
    if (child.kind === "file") {
      folder.size += child.size;
      folder.fileIndexes.push(child.index);
    } else {
      finalizeFolder(child);
      folder.size += child.size;
      folder.fileIndexes.push(...child.fileIndexes);
    }
  }
}

/** 由平铺文件路径构建目录树（V2 预览契约只有 files[].path，树在前端组装）。 */
function buildTree(files: TorrentFilePreviewResponse[]): TreeNode[] {
  const root: TreeFolder = { kind: "folder", name: "", children: [], fileIndexes: [], size: 0 };
  for (const file of files) {
    const segments = file.path.split("/").filter(Boolean);
    let node = root;
    for (let depth = 0; depth < segments.length - 1; depth += 1) {
      const name = segments[depth];
      let child = node.children.find(
        (candidate): candidate is TreeFolder =>
          candidate.kind === "folder" && candidate.name === name,
      );
      if (child === undefined) {
        child = { kind: "folder", name, children: [], fileIndexes: [], size: 0 };
        node.children.push(child);
      }
      node = child;
    }
    node.children.push({
      kind: "file",
      name: segments[segments.length - 1] ?? file.path,
      index: file.index,
      size: file.size,
    });
  }
  finalizeFolder(root);
  return root.children;
}

function nodeMatches(node: TreeNode, query: string): boolean {
  if (query === "") return true;
  if (node.name.toLowerCase().includes(query)) return true;
  return node.kind === "folder" && node.children.some((child) => nodeMatches(child, query));
}

function nodeHasSelectedLeaf(node: TreeNode, selected: Set<number>): boolean {
  if (node.kind === "file") return selected.has(node.index);
  return node.children.some((child) => nodeHasSelectedLeaf(child, selected));
}

function folderState(
  indexes: number[],
  selected: Set<number>,
): "checked" | "partial" | "empty" {
  const count = indexes.filter((index) => selected.has(index)).length;
  if (count === 0) return "empty";
  return count === indexes.length ? "checked" : "partial";
}

/** 半选勾选框：原生 checkbox + indeterminate（React 无对应属性，用 ref 写入）。 */
function TriStateCheckbox({
  state,
  label,
  disabled,
  onChange,
}: {
  state: "checked" | "partial" | "empty";
  label: string;
  disabled?: boolean;
  onChange: () => void;
}) {
  return (
    <input
      type="checkbox"
      aria-label={label}
      checked={state === "checked"}
      disabled={disabled}
      onChange={onChange}
      ref={(element) => {
        if (element !== null) element.indeterminate = state === "partial";
      }}
    />
  );
}

function TreeRow({
  node,
  depth,
  selected,
  query,
  readonly,
  onToggleFile,
  onToggleFolder,
}: {
  node: TreeNode;
  depth: number;
  selected: Set<number>;
  query: string;
  readonly: boolean;
  onToggleFile: (index: number) => void;
  onToggleFolder: (indexes: number[]) => void;
}) {
  if (readonly && !nodeHasSelectedLeaf(node, selected)) return null;
  if (!nodeMatches(node, query)) return null;

  if (node.kind === "file") {
    const state = selected.has(node.index) ? "checked" : "empty";
    if (readonly && state === "empty") return null;
    return (
      <div className="torrent-tree-row" style={{ paddingLeft: 8 + depth * 18 }}>
        {readonly ? (
          <span className="torrent-tree-mark" aria-hidden="true">✓</span>
        ) : (
          <TriStateCheckbox
            state={state}
            label={`选择文件 ${node.name}`}
            onChange={() => onToggleFile(node.index)}
          />
        )}
        <span className="torrent-tree-name" title={node.name}>
          {node.name}
        </span>
        <span className="torrent-tree-size">{formatBytes(node.size)}</span>
      </div>
    );
  }

  const state = folderState(node.fileIndexes, selected);
  return (
    <>
      <div className="torrent-tree-row torrent-tree-row-folder" style={{ paddingLeft: 8 + depth * 18 }}>
        {readonly ? (
          <span className="torrent-tree-mark" aria-hidden="true">✓</span>
        ) : (
          <TriStateCheckbox
            state={state}
            label={`选择目录 ${node.name}`}
            onChange={() => onToggleFolder(node.fileIndexes)}
          />
        )}
        <span className="torrent-tree-name" title={node.name}>
          {node.name}
        </span>
        <span className="torrent-tree-size">{formatBytes(node.size)}</span>
      </div>
      {node.children.map((child) => (
        <TreeRow
          key={child.kind === "file" ? `f-${child.index}` : `d-${child.name}`}
          node={child}
          depth={depth + 1}
          selected={selected}
          query={query}
          readonly={readonly}
          onToggleFile={onToggleFile}
          onToggleFolder={onToggleFolder}
        />
      ))}
    </>
  );
}

function TorrentTree({
  nodes,
  selected,
  searchQuery,
  readonly,
  onToggleFile,
  onToggleFolder,
}: {
  nodes: TreeNode[];
  selected: Set<number>;
  searchQuery: string;
  readonly: boolean;
  onToggleFile: (index: number) => void;
  onToggleFolder: (indexes: number[]) => void;
}) {
  const query = searchQuery.trim().toLowerCase();
  const hasVisible = nodes.some(
    (node) => nodeMatches(node, query) && (!readonly || nodeHasSelectedLeaf(node, selected)),
  );
  return (
    <div className="torrent-tree">
      {hasVisible ? (
        nodes.map((node) => (
          <TreeRow
            key={node.kind === "file" ? `f-${node.index}` : `d-${node.name}`}
            node={node}
            depth={0}
            selected={selected}
            query={query}
            readonly={readonly}
            onToggleFile={onToggleFile}
            onToggleFolder={onToggleFolder}
          />
        ))
      ) : (
        <div className="torrent-tree-empty">没有匹配文件</div>
      )}
    </div>
  );
}

type WizardStage = "select" | "confirm";

/** 种子创建三阶段向导（T8）：上传解析已完成 → 选择文件（树形半选/过滤/全选清空/摘要）→ 确认创建。 */
export function TorrentCreateWizard({
  preview,
  onClose,
  onCreate,
}: {
  preview: TorrentPreviewResponse;
  onClose: () => void;
  onCreate: (selected: number[]) => Promise<void>;
}) {
  // 默认勾选保持服务端 default_selection（T9：不回退旧版全选语义）。
  const [selected, setSelected] = useState<Set<number>>(() => new Set(preview.default_selection));
  const [stage, setStage] = useState<WizardStage>("select");
  const [searchQuery, setSearchQuery] = useState("");
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tree = useMemo(() => buildTree(preview.files), [preview.files]);
  const selectedSize = useMemo(
    () =>
      preview.files.reduce(
        (sum, file) => (selected.has(file.index) ? sum + file.size : sum),
        0,
      ),
    [preview.files, selected],
  );

  const toggleFile = (index: number) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const toggleFolder = (indexes: number[]) => {
    setSelected((current) => {
      const next = new Set(current);
      const allSelected = indexes.every((index) => next.has(index));
      for (const index of indexes) {
        if (allSelected) next.delete(index);
        else next.add(index);
      }
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(preview.files.map((file) => file.index)));
  const clearAll = () => setSelected(new Set());

  const requestCancel = () => {
    // 已有选择时二次确认，避免误触丢掉勾选结果。
    if (selected.size > 0) setConfirmingCancel(true);
    else onClose();
  };

  const submit = async () => {
    if (submitting || selected.size === 0) return;
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
      onClose={requestCancel}
      footer={
        confirmingCancel ? (
          <>
            <span className="torrent-cancel-text">取消添加任务？当前文件选择不会保存。</span>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setConfirmingCancel(false)}
            >
              继续选择
            </button>
            <button type="button" className="danger-button" onClick={onClose}>
              确认取消
            </button>
          </>
        ) : stage === "select" ? (
          <>
            <span className="torrent-summary" aria-live="polite">
              已选 {selected.size} 项 · {formatBytes(selectedSize)}
            </span>
            <button type="button" className="secondary-button" onClick={requestCancel}>
              取消
            </button>
            <button
              type="button"
              className="primary-button"
              disabled={selected.size === 0}
              onClick={() => setStage("confirm")}
            >
              下一阶段
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="secondary-button"
              disabled={submitting}
              onClick={() => setStage("select")}
            >
              上一步
            </button>
            <button
              type="button"
              className="primary-button"
              disabled={submitting}
              onClick={() => void submit()}
            >
              {submitting ? "创建中…" : `确认创建（${selected.size} 项）`}
            </button>
          </>
        )
      }
    >
      {error !== null && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      <ol className="torrent-stages" aria-label="创建阶段">
        <li className="done">1. 上传解析</li>
        <li className={stage === "select" ? "active" : "done"}>2. 选择文件</li>
        <li className={stage === "confirm" ? "active" : ""}>3. 确认下载</li>
      </ol>
      {stage === "select" ? (
        <>
          <div className="torrent-toolbar">
            <input
              type="search"
              placeholder="搜索文件"
              aria-label="搜索文件"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
            <div className="torrent-toolbar-actions">
              <button type="button" className="secondary-button" onClick={selectAll}>
                全选
              </button>
              <button type="button" className="secondary-button" onClick={clearAll}>
                清空
              </button>
            </div>
          </div>
          <TorrentTree
            nodes={tree}
            selected={selected}
            searchQuery={searchQuery}
            readonly={false}
            onToggleFile={toggleFile}
            onToggleFolder={toggleFolder}
          />
        </>
      ) : (
        <>
          <p>
            确认下载内容：{selected.size} 个文件 · {formatBytes(selectedSize)}
          </p>
          <TorrentTree
            nodes={tree}
            selected={selected}
            searchQuery=""
            readonly
            onToggleFile={toggleFile}
            onToggleFolder={toggleFolder}
          />
        </>
      )}
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
