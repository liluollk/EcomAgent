import { useState } from "react";
import type { ExecutionMode, WorkspaceDetailView, WorkspaceRole } from "./types";

export interface WorkspacePageProps {
  currentRole: WorkspaceRole;
  workspace: WorkspaceDetailView;
  onSaveExecutionMode?: (workspaceId: string, mode: ExecutionMode) => Promise<void> | void;
}

const executionModeLabels: Record<ExecutionMode, string> = {
  AUTO_EXECUTE: "自动执行(AUTO_EXECUTE)",
  AWAIT_CONFIRMATION: "等待确认(AWAIT_CONFIRMATION)",
};

const executionModeOptions: ExecutionMode[] = ["AUTO_EXECUTE", "AWAIT_CONFIRMATION"];

export function WorkspacePage({
  currentRole,
  workspace,
  onSaveExecutionMode,
}: WorkspacePageProps) {
  const isOwner = currentRole === "OWNER";
  const [editingMode, setEditingMode] = useState(false);
  const [selectedMode, setSelectedMode] = useState<ExecutionMode>(workspace.executionMode);
  const [saving, setSaving] = useState(false);

  const startEditing = () => {
    setSelectedMode(workspace.executionMode);
    setEditingMode(true);
  };

  const saveMode = async () => {
    if (!onSaveExecutionMode) {
      setEditingMode(false);
      return;
    }
    setSaving(true);
    try {
      await onSaveExecutionMode(workspace.id, selectedMode);
      setEditingMode(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section aria-label={`Workspace ${workspace.name}`}>
      <h1>{workspace.name}</h1>
      <p>同一 Workspace 内多店并列展示,不以单个店铺作为全局上下文。</p>

      <section aria-label="店铺">
        <h2>店铺</h2>
        <ul>
          {workspace.stores.map((store) => (
            <li key={store.id}>
              <span>{store.name}</span>
              <span>{store.platform}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Agent">
        <h2>Agent</h2>
        <ul>
          {workspace.agents.map((agent) => (
            <li key={agent.name}>
              <span>{agent.name}</span>
              <span>{agent.agentVersion}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Model">
        <h2>Model</h2>
        <ul>
          {workspace.models.map((model) => (
            <li key={model.name}>
              <span>{model.name}</span>
              <span>{model.provider}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Skill">
        <h2>Skill</h2>
        <ul>
          {workspace.skills.map((skill) => (
            <li key={skill.name}>
              <span>{skill.name}</span>
              <span>{skill.skillVersion}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Source">
        <h2>Source</h2>
        <ul>
          {workspace.sources.map((source) => (
            <li key={source.name}>
              <span>{source.name}</span>
              <span>{source.kind}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="执行模式">
        <h2>执行模式</h2>
        {editingMode ? (
          <div>
            <fieldset>
              <legend>选择执行模式</legend>
              {executionModeOptions.map((mode) => (
                <label key={mode}>
                  <input
                    type="radio"
                    name="execution-mode"
                    value={mode}
                    checked={selectedMode === mode}
                    onChange={() => setSelectedMode(mode)}
                  />
                  {mode}
                </label>
              ))}
            </fieldset>
            <button type="button" onClick={saveMode} disabled={saving}>
              保存
            </button>
            <button type="button" onClick={() => setEditingMode(false)}>
              取消
            </button>
          </div>
        ) : (
          <div>
            <p>当前执行模式:{executionModeLabels[workspace.executionMode]}</p>
            {isOwner ? (
              <button type="button" onClick={startEditing}>
                编辑执行模式
              </button>
            ) : (
              <p>当前为只读视图,执行模式仅 Owner 可修改。</p>
            )}
          </div>
        )}
      </section>

      <section aria-label="成员与资源范围">
        <h2>成员与资源范围</h2>
        <ul>
          {workspace.members.map((member) => (
            <li key={member.id}>
              <span>{member.displayName}</span>
              <span>{member.role}</span>
              <span>资源范围:{member.resourceScopes.join("、")}</span>
            </li>
          ))}
        </ul>
      </section>
    </section>
  );
}
