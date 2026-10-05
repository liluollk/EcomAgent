import { TaskComposer } from "./TaskComposer";
import { WorkspaceContainer } from "../features/workspaces/WorkspaceContainer";

export interface WorkspaceViewProps {
  workspaceId: string;
  currentRole: "OWNER" | "OPERATOR";
  onSessionOpened: (sessionId: string) => void;
}

/** 工作台主视图:新任务编辑器 + Workspace 配置页。 */
export function WorkspaceView({
  workspaceId,
  currentRole,
  onSessionOpened,
}: WorkspaceViewProps) {
  return (
    <div>
      <TaskComposer workspaceId={workspaceId} onSessionOpened={onSessionOpened} />
      <WorkspaceContainer currentRole={currentRole} workspaceId={workspaceId} />
    </div>
  );
}
