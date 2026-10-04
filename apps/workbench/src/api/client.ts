const API_BASE = "/api";

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

interface ErrorBody {
  code?: string;
  message?: string;
}

async function request<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

  if (!response.ok) {
    const errorBody = (await response.json().catch(() => null)) as ErrorBody | null;
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
