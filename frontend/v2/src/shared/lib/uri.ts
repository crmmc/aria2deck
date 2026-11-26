/** 种子任务的展示 uri 是 `torrent:<info_hash>` 伪协议：复制时只给 info hash，其他工具才认。 */
export function copyTargetUri(uri: string): string {
  return uri.startsWith("torrent:") ? uri.slice("torrent:".length) : uri;
}
