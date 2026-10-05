import { useCallback, useEffect, useState } from "react";
import { getJSON, patchJSON } from "../../api/client";
import type {
  AgentView,
  ExecutionMode,
  MemberView,
  ModelView,
  SkillView,
  SourceView,
  StoreView,
  WorkspaceDetailView,
  WorkspaceRole,
} from "./types";
import { WorkspacePage } from "./WorkspacePage";

export interface WorkspaceContainerProps {
  workspaceId: string;
  currentRole: WorkspaceRole;
}

interface WorkspaceApiResponse {
  id: string;
  name: string;
  status: string;
  execution_mode?: string;
  stores?: StoreView[];
  agents?: AgentView[];
  models?: ModelView[];
  skills?: SkillView[];
  sources?: SourceView[];
  members?: MemberView[];
}

/**
 * 后端 WorkspaceView 目前只返回 id/name/status/execution_mode 等基础字段,
 * 店铺/Agent/Model 等分节来自后续配置 API;缺失的分节以空列表渲染,
 * 保证真实 API 下工作台页面可用。
 */
function normalizeWorkspace(detail: WorkspaceApiResponse): WorkspaceDetailView {
  return {
    id: detail.id,
    name: detail.name,
    executionMode: (detail.execution_mode ?? "AWAIT_CONFIRMATION") as ExecutionMode,
    stores: detail.stores ?? [],
    agents: detail.agents ?? [],
    models: detail.models ?? [],
    skills: detail.skills ?? [],
    sources: detail.sources ?? [],
    members: detail.members ?? [],
  };
}

export function WorkspaceContainer({ workspaceId, currentRole }: WorkspaceContainerProps) {
  const [workspace, setWorkspace] = useState<WorkspaceDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await getJSON<WorkspaceApiResponse>(`/workspaces/${workspaceId}`);
      setWorkspace(normalizeWorkspace(detail));
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [workspaceId]);

  useEffect(() => {
    // 数据加载型 effect:setState 发生在异步回调里,非同步级联渲染。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const saveExecutionMode = useCallback(
    async (id: string, mode: ExecutionMode) => {
      await patchJSON(`/workspaces/${id}/execution-mode`, { mode });
      await load();
    },
    [load],
  );

  if (error) {
    return <div role="alert">工作台加载失败:{error}</div>;
  }
  if (!workspace) {
    return <p>正在加载 Workspace…</p>;
  }

  return (
    <WorkspacePage
      currentRole={currentRole}
      workspace={workspace}
      onSaveExecutionMode={saveExecutionMode}
    />
  );
}
