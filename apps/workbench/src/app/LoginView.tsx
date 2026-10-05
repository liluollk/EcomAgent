import { useState } from "react";

import { postJSON, setStoredToken } from "../api/client";

interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface LoginViewProps {
  onLoggedIn: (token: string) => void;
}

/** 登录视图:本地账号登录,签发的短期 JWT 存入 sessionStorage。 */
export function LoginView({ onLoggedIn }: LoginViewProps) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (submitEvent: React.FormEvent) => {
    submitEvent.preventDefault();
    if (!login.trim() || !password || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await postJSON<LoginResponse>("/auth/login", {
        login: login.trim(),
        password,
      });
      setStoredToken(response.access_token);
      onLoggedIn(response.access_token);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section aria-label="登录">
      <h1>Agent 工作台</h1>
      <form onSubmit={submit}>
        <label>
          登录名
          <input
            aria-label="登录名"
            value={login}
            autoComplete="username"
            onChange={(changeEvent) => setLogin(changeEvent.target.value)}
          />
        </label>
        <label>
          密码
          <input
            aria-label="密码"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(changeEvent) => setPassword(changeEvent.target.value)}
          />
        </label>
        <button type="submit" disabled={submitting}>
          登录
        </button>
        {error && (
          <p role="alert">{error}</p>
        )}
      </form>
    </section>
  );
}
