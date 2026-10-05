import { useCallback, useEffect, useState } from "react";

import { getJSON, getStoredToken } from "../api/client";
import { SessionsPage } from "../features/sessions/SessionsPage";
import { SessionPage } from "../features/sessions/SessionPage";
import { LoginView } from "./LoginView";
import { WorkspaceView } from "./WorkspaceView";

type FeatureKey = "workspace" | "sessions" | "settings";

interface WorkspaceSummary {
  id: string;
  name: string;
}

const sidebarFeatures: Array<{ key: FeatureKey; label: string }> = [
  { key: "workspace", label: "Workspace" },
  { key: "sessions", label: "Sessions" },
  { key: "settings", label: "Settings" },
];

function readInitialSessionId(): string | null {
  try {
    return new URLSearchParams(window.location.search).get("session");
  } catch {
    return null;
  }
}

/**
 * 应用 Shell(阶段 4 Gate 补齐登录与工作区选择):
 *
 * - 无登录态 → 登录视图;登录后拉取当前 actor 的工作区列表;
 * - Workspace 视图:工作区选择 → 新任务编辑器 + 工作区配置页;
 * - 会话视图:?session=<id> 深链直达(刷新后仍停留在该会话),
 *   打开会话时以 replaceState 同步地址,不引入路由依赖;
 * - 不建立全局单店上下文,Workspace 内多店并列展示。
 */
export function App() {
  const [token, setToken] = useState<string | null>(() => getStoredToken());
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceSummary | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(() => readInitialSessionId());
  const [activeFeature, setActiveFeature] = useState<FeatureKey>("workspace");

  useEffect(() => {
    if (token === null || workspaces !== null) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const response = await getJSON<{ workspaces: WorkspaceSummary[] }>("/workspaces");
        if (!cancelled) {
          setWorkspaces(response.workspaces);
          setWorkspaceError(null);
        }
      } catch (cause) {
        if (!cancelled) {
          setWorkspaces([]);
          setWorkspaceError(cause instanceof Error ? cause.message : String(cause));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, workspaces]);

  const openSession = useCallback((id: string) => {
    setSessionId(id);
    try {
      window.history.replaceState(null, "", `/?session=${encodeURIComponent(id)}`);
    } catch {
      // 地址栏不可写(如 file://)时忽略:会话仍在当前视图中打开。
    }
  }, []);

  const leaveSession = useCallback(() => {
    setSessionId(null);
    try {
      window.history.replaceState(null, "", "/");
    } catch {
      // 同上:忽略地址栏更新失败。
    }
  }, []);

  if (token === null) {
    return <LoginView onLoggedIn={setToken} />;
  }

  return (
    <div className="app-shell">
      <nav aria-label="功能导航">
        {sidebarFeatures.map((feature) => (
          <button
            key={feature.key}
            type="button"
            onClick={() => {
              setActiveFeature(feature.key);
              if (feature.key !== "sessions") {
                leaveSession();
              }
            }}
            aria-current={activeFeature === feature.key ? "true" : undefined}
          >
            {feature.label}
          </button>
        ))}
        {activeWorkspace !== null && (
          <button
            type="button"
            onClick={() => {
              setActiveWorkspace(null);
              leaveSession();
            }}
          >
            切换工作区
          </button>
        )}
      </nav>
      <main>
        {sessionId !== null ? (
          <div>
            <button type="button" onClick={leaveSession}>
              返回工作区
            </button>
            <SessionPage sessionId={sessionId} />
          </div>
        ) : activeFeature === "workspace" ? (
          activeWorkspace !== null ? (
            <WorkspaceView
              workspaceId={activeWorkspace.id}
              onSessionOpened={openSession}
            />
          ) : (
            <section aria-label="选择工作区">
              <h1>选择工作区</h1>
              {workspaceError && <p role="alert">{workspaceError}</p>}
              {workspaces === null ? (
                <p>正在加载工作区…</p>
              ) : workspaces.length === 0 ? (
                <p>当前账号没有可访问的工作区。</p>
              ) : (
                workspaces.map((workspace) => (
                  <button
                    key={workspace.id}
                    type="button"
                    onClick={() => setActiveWorkspace(workspace)}
                  >
                    {workspace.name}
                  </button>
                ))
              )}
            </section>
          )
        ) : activeFeature === "sessions" ? (
          <SessionsPage onSessionOpened={openSession} />
        ) : (
          <p>Settings 页面将在后续任务实现。</p>
        )}
      </main>
    </div>
  );
}
