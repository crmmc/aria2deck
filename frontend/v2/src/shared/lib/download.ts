/** WEB-12 三类下载触发：普通文件 a[download]、批量隐藏 iframe、公开页 POST form。
 * Toast 只表示「已触发」，不代表磁盘下载完成。 */

const BATCH_INTERVAL_MS = 800;
const IFRAME_CLEANUP_MS = 5000;

/** 普通文件：a[download] 触发浏览器下载（Cookie 会话）。 */
export function triggerFileDownload(url: string, filename?: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.rel = "noopener";
  if (filename !== undefined) anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

/** 批量：隐藏 iframe 逐个触发，间隔 800ms，避免并发会话互相打断。 */
export async function triggerBatchDownloads(
  urls: string[],
  intervalMs: number = BATCH_INTERVAL_MS,
): Promise<void> {
  for (const [index, url] of urls.entries()) {
    const iframe = document.createElement("iframe");
    iframe.style.display = "none";
    iframe.src = url;
    document.body.appendChild(iframe);
    window.setTimeout(() => iframe.remove(), IFRAME_CLEANUP_MS);
    if (index < urls.length - 1) {
      await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
    }
  }
}

/** 公开分享页：POST 表单提交（有密码分享令牌放隐藏域，绝不进 URL；无密码直连可省略）。 */
export function triggerFormDownload(
  action: string,
  fields: { token?: string; path?: string },
): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = action;
  form.style.display = "none";
  if (fields.token !== undefined) {
    const tokenInput = document.createElement("input");
    tokenInput.type = "hidden";
    tokenInput.name = "token";
    tokenInput.value = fields.token;
    form.appendChild(tokenInput);
  }
  if (fields.path !== undefined) {
    const pathInput = document.createElement("input");
    pathInput.type = "hidden";
    pathInput.name = "path";
    pathInput.value = fields.path;
    form.appendChild(pathInput);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
}
