import type { ApiError } from "./types";

/** 客户端唯一 fetch 入口：统一 headers、错误解析、401 登出回调、网络异常中文映射。 */

export class ApiRequestError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

type UnauthorizedHandler = () => void;

let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

const NETWORK_MESSAGE = "网络异常，请检查连接后重试";

/** 网络健康信号（G9）：任何请求网络异常置位、任何响应到达清除；布局横幅订阅。 */
type NetworkHealthListener = () => void;

const healthListeners = new Set<NetworkHealthListener>();
let networkOffline = false;

export function subscribeNetworkHealth(listener: NetworkHealthListener): () => void {
  healthListeners.add(listener);
  return () => healthListeners.delete(listener);
}

export function getNetworkOffline(): boolean {
  return networkOffline;
}

function markNetworkHealth(offline: boolean): void {
  if (networkOffline === offline) return;
  networkOffline = offline;
  for (const listener of healthListeners) listener();
}

export interface RequestOptions {
  /** 附加请求头（公开分享页的 Bearer 访问令牌）。 */
  headers?: Record<string, string>;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...options?.headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    markNetworkHealth(true);
    throw new ApiRequestError(0, "network_error", NETWORK_MESSAGE);
  }
  // 能拿到响应（无论状态码）即证明网络连通。
  markNetworkHealth(false);

  if (response.status === 401) {
    // 带 Bearer 令牌的是公开分享访问，401 属于页面内业务结果，不触发会话登出。
    if (options?.headers?.Authorization === undefined) onUnauthorized?.();
    const error = await parseError(response);
    throw new ApiRequestError(401, error?.code ?? "unauthenticated", error?.message ?? "请先登录");
  }

  if (!response.ok) {
    const error = await parseError(response);
    throw new ApiRequestError(
      response.status,
      error?.code ?? `http_${response.status}`,
      error?.message ?? httpFallbackMessage(response.status),
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function parseError(response: Response): Promise<ApiError | null> {
  try {
    const payload = (await response.json()) as { error?: ApiError };
    if (payload?.error && typeof payload.error.code === "string") {
      return payload.error;
    }
    return null;
  } catch {
    return null;
  }
}

function httpFallbackMessage(status: number): string {
  if (status >= 500) return "服务器错误，请稍后重试";
  if (status === 404) return "资源不存在";
  if (status === 403) return "没有执行该操作的权限";
  if (status === 429) return "请求过于频繁，请稍后重试";
  if (status >= 400) return "请求无效";
  return `请求失败（${status}）`;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>("GET", path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("POST", path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PUT", path, body, options),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PATCH", path, body, options),
  del: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("DELETE", path, body, options),
};

/** 统一错误文案：API 失败必为 ApiRequestError（Error 子类，中文 message），其余异常回退固定文案。 */
export function errorMessage(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}
