type TriggerDownloadsOptions = {
  delayMs?: number;
  cleanupMs?: number;
};

const DEFAULT_DELAY_MS = 1200;
const DEFAULT_CLEANUP_MS = 5000;

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function triggerDownload(url: string, cleanupMs: number) {
  const iframe = document.createElement("iframe");
  iframe.style.display = "none";
  iframe.src = url;
  document.body.appendChild(iframe);

  setTimeout(() => {
    iframe.remove();
  }, cleanupMs);
}

export async function triggerDownloadsSequentially(
  urls: string[],
  {
    delayMs = DEFAULT_DELAY_MS,
    cleanupMs = DEFAULT_CLEANUP_MS,
  }: TriggerDownloadsOptions = {},
) {
  for (let index = 0; index < urls.length; index += 1) {
    triggerDownload(urls[index], cleanupMs);

    if (index < urls.length - 1) {
      await delay(delayMs);
    }
  }
}
