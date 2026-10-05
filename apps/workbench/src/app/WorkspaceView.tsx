import { WorkspaceContainer } from "../features/workspaces/WorkspaceContainer";
import { WorkspacePage } from "../features/workspaces/WorkspacePage";
import { TaskComposer } from "./TaskComposer";

export interface WorkspaceViewProps {
  workspaceId: string;
  onSessionOpened: (sessionId: string) => void;
}

/** 工作台主视图:新任务编辑器 + Workspace 配置页。 */
export function WorkspaceView({
  workspaceId,
  onSessionOpened,
}: WorkspaceViewProps) {
  return (
    <div>
      <WorkspaceContainer workspaceId={workspaceId}>
        {({ overview, saveExecutionMode }) => (
          <>
            <TaskComposer
              workspaceId={overview.workspace.id}
              agent={overview.agent}
              onSessionOpened={onSessionOpened}
            />
            <WorkspacePage overview={overview} onSaveExecutionMode={saveExecutionMode} />
          </>
        )}
      </WorkspaceContainer>
    </div>
  );
}
