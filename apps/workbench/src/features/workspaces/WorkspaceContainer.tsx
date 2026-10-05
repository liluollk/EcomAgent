import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { getJSON, patchJSON } from "../../api/client";
import type { ExecutionMode, WorkspaceOverview } from "./types";
import { WorkspacePage } from "./WorkspacePage";

export interface WorkspaceContainerProps {
  workspaceId: string;
  children?: (data: {
    overview: WorkspaceOverview;
    saveExecutionMode: (workspaceId: string, mode: ExecutionMode) => Promise<void>;
  }) => ReactNode;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseWorkspaceOverview(value: unknown): WorkspaceOverview {
  if (
    !isRecord(value) ||
    !isRecord(value.workspace) ||
    !isRecord(value.current_membership) ||
    !Array.isArray(value.stores) ||
    !Array.isArray(value.model_connections) ||
    !Array.isArray(value.skills) ||
    !Array.isArray(value.sources) ||
    !Array.isArray(value.business_policies) ||
    !(value.agent === null || isRecord(value.agent))
  ) {
    throw new Error("工作台数据格式不完整");
  }
  const { workspace, current_membership: membership } = value;
  if (
    typeof workspace.id !== "string" ||
    typeof workspace.name !== "string" ||
    !["READ_ONLY", "CONFIRM_WRITES", "AUTO_EXECUTE"].includes(
      String(workspace.execution_mode),
    ) ||
    typeof membership.id !== "string" ||
    !["WORKSPACE_OWNER", "OPERATOR"].includes(String(membership.role))
  ) {
    throw new Error("工作台数据格式不完整");
  }
  return value as unknown as WorkspaceOverview;
}

export function WorkspaceContainer({
  workspaceId,
  children,
}: WorkspaceContainerProps) {
  const [overview, setOverview] = useState<WorkspaceOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await getJSON<unknown>(`/workspaces/${workspaceId}/overview`);
      setOverview(parseWorkspaceOverview(response));
      setError(null);
    } catch (cause) {
      setOverview(null);
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
  if (!overview) {
    return <p>正在加载 Workspace…</p>;
  }

  if (children) {
    return children({ overview, saveExecutionMode });
  }

  return (
    <WorkspacePage overview={overview} onSaveExecutionMode={saveExecutionMode} />
  );
}
