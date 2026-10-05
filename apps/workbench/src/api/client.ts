const API_BASE = "/api";

const TOKEN_STORAGE_KEY = "workbench.token";
/** 登录 JWT 的会话内存储 Key(page.reload 后仍有效,关标签页即失效)。 */

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(status: number, code: string | undefined, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function getStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredToken(token: string | null): void {
  try {
    if (token === null) {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } else {
      sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    }
  } catch {
    // 存储不可用(如隐私模式):仅本次会话内存有效。
  }
}

interface ErrorBody {
  code?: string;
  message?: string;
}

async function request<T>(path: string, method: string, body?: unknown): Promise<T> {
  const token = getStoredToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

  if (!response.ok) {
    const errorBody = (await response.json().catch(() => null)) as ErrorBody | null;
    if (response.status === 401) {
      // 令牌失效:清除登录态,让应用回到登录视图。
      setStoredToken(null);
    }
    throw new ApiError(
      response.status,
      errorBody?.code,
      errorBody?.message ?? `API 请求失败:${response.status}`,
    );
  }

  return (await response.json()) as T;
}

export function getJSON<T>(path: string): Promise<T> {
  return request<T>(path, "GET");
}

export function postJSON<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, "POST", body);
}

export function patchJSON<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, "PATCH", body);
}
