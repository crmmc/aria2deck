import { formatBytes } from "@/lib/utils";
import type { TorrentTreeNode } from "@/types";

type TorrentFileTreeProps = {
  nodes: TorrentTreeNode[];
  selectedIndexes: Set<number>;
  searchQuery: string;
  readonly?: boolean;
  onToggleFile?: (index: number) => void;
  onToggleDirectory?: (indexes: number[]) => void;
};

function collectIndexes(node: TorrentTreeNode): number[] {
  if (node.type === "file") return [node.index];
  return node.children.flatMap(collectIndexes);
}

function nodeMatches(node: TorrentTreeNode, query: string): boolean {
  if (!query) return true;
  if (node.name.toLowerCase().includes(query)) return true;
  return node.type === "directory" && node.children.some((child) => nodeMatches(child, query));
}

function nodeHasSelectedLeaf(node: TorrentTreeNode, selectedIndexes: Set<number>): boolean {
  if (node.type === "file") return selectedIndexes.has(node.index);
  return node.children.some((child) => nodeHasSelectedLeaf(child, selectedIndexes));
}

function directoryState(
  indexes: number[],
  selectedIndexes: Set<number>
): "checked" | "partial" | "empty" {
  const selectedCount = indexes.filter((index) => selectedIndexes.has(index)).length;
  if (selectedCount === 0) return "empty";
  if (selectedCount === indexes.length) return "checked";
  return "partial";
}

function CheckMark({ state }: { state: "checked" | "partial" | "empty" }) {
  return (
    <span className={`torrent-check torrent-check-${state}`} aria-hidden="true">
      {state === "checked" ? "✓" : state === "partial" ? "-" : ""}
    </span>
  );
}

function TreeNode({
  node,
  depth,
  selectedIndexes,
  query,
  readonly,
  onToggleFile,
  onToggleDirectory,
}: {
  node: TorrentTreeNode;
  depth: number;
  selectedIndexes: Set<number>;
  query: string;
  readonly: boolean;
  onToggleFile?: (index: number) => void;
  onToggleDirectory?: (indexes: number[]) => void;
}) {
  if (readonly && !nodeHasSelectedLeaf(node, selectedIndexes)) return null;
  if (!nodeMatches(node, query)) return null;

  if (node.type === "file") {
    const checked = selectedIndexes.has(node.index);
    if (readonly && !checked) return null;
    return (
      <div className="torrent-tree-row" style={{ paddingLeft: 12 + depth * 24 }}>
        <span className="torrent-tree-toggle" />
        {readonly ? (
          <span className="torrent-check torrent-check-checked" aria-hidden="true">
            ✓
          </span>
        ) : (
          <button
            type="button"
            className="torrent-check-button"
            role="checkbox"
            aria-checked={checked}
            aria-label={node.name}
            onClick={() => onToggleFile?.(node.index)}
          >
            <CheckMark state={checked ? "checked" : "empty"} />
          </button>
        )}
        <span className="torrent-tree-name">{node.name}</span>
        <span className="torrent-tree-size">{formatBytes(node.size)}</span>
      </div>
    );
  }

  const indexes = collectIndexes(node);
  const state = directoryState(indexes, selectedIndexes);
  return (
    <>
      <div
        className="torrent-tree-row torrent-tree-row-folder"
        style={{ paddingLeft: 12 + depth * 24 }}
      >
        <span className="torrent-tree-toggle">▾</span>
        {readonly ? (
          <span className="torrent-check torrent-check-checked" aria-hidden="true">
            ✓
          </span>
        ) : (
          <button
            type="button"
            className="torrent-check-button"
            role="checkbox"
            aria-checked={state === "partial" ? "mixed" : state === "checked"}
            aria-label={node.name}
            onClick={() => onToggleDirectory?.(indexes)}
          >
            <CheckMark state={state} />
          </button>
        )}
        <span className="torrent-tree-name">{node.name}</span>
        <span className="torrent-tree-size">{formatBytes(node.size)}</span>
      </div>
      {node.children.map((child) => (
        <TreeNode
          key={child.path.join("/")}
          node={child}
          depth={depth + 1}
          selectedIndexes={selectedIndexes}
          query={query}
          readonly={readonly}
          onToggleFile={onToggleFile}
          onToggleDirectory={onToggleDirectory}
        />
      ))}
    </>
  );
}

export function TorrentFileTree({
  nodes,
  selectedIndexes,
  searchQuery,
  readonly = false,
  onToggleFile,
  onToggleDirectory,
}: TorrentFileTreeProps) {
  const query = searchQuery.trim().toLowerCase();
  const hasVisibleNodes = nodes.some(
    (node) => nodeMatches(node, query) && (!readonly || nodeHasSelectedLeaf(node, selectedIndexes))
  );

  return (
    <div className="torrent-tree-frame">
      <div className="torrent-tree-head">
        <span />
        <span />
        <span>文件</span>
        <span>大小</span>
      </div>
      <div className="torrent-tree-scroll">
        {hasVisibleNodes ? (
          nodes.map((node) => (
            <TreeNode
              key={node.path.join("/")}
              node={node}
              depth={0}
              selectedIndexes={selectedIndexes}
              query={query}
              readonly={readonly}
              onToggleFile={onToggleFile}
              onToggleDirectory={onToggleDirectory}
            />
          ))
        ) : (
          <div className="torrent-tree-empty">没有匹配文件</div>
        )}
      </div>
    </div>
  );
}
