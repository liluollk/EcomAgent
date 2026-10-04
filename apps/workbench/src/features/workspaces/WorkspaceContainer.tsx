import { useCallback, useEffect, useState } from "react";
import { getJSON, patchJSON } from "../../api/client";
import type { ExecutionMode, WorkspaceDetailView, WorkspaceRole } from "./types";
import { WorkspacePage } from "./WorkspacePage";

export interface WorkspaceContainerProps {
  workspaceId: string;
  currentRole: WorkspaceRole;
}

export function WorkspaceContainer({ workspaceId, currentRole }: WorkspaceContainerProps) {
  const [workspace, setWorkspace] = useState<WorkspaceDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await getJSON<WorkspaceDetailView>(`/workspaces/${workspaceId}`);
      setWorkspace(detail);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [workspaceId]);

  useEffect(() => {
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
