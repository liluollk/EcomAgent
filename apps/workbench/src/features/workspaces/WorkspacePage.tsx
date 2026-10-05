import { useState } from "react";
import type { ExecutionMode, WorkspaceOverview } from "./types";

export interface WorkspacePageProps {
  overview: WorkspaceOverview;
  onSaveExecutionMode?: (workspaceId: string, mode: ExecutionMode) => Promise<void> | void;
}

const executionModeLabels: Record<ExecutionMode, string> = {
  READ_ONLY: "只读(READ_ONLY)",
  CONFIRM_WRITES: "写操作需确认(CONFIRM_WRITES)",
  AUTO_EXECUTE: "自动执行(AUTO_EXECUTE)",
};

const executionModeOptions: ExecutionMode[] = [
  "READ_ONLY",
  "CONFIRM_WRITES",
  "AUTO_EXECUTE",
];

export function WorkspacePage({ overview, onSaveExecutionMode }: WorkspacePageProps) {
  const workspace = overview.workspace;
  const isOwner = overview.current_membership.role === "WORKSPACE_OWNER";
  const [editingMode, setEditingMode] = useState(false);
  const [selectedMode, setSelectedMode] = useState<ExecutionMode>(workspace.execution_mode);
  const [saving, setSaving] = useState(false);

  const startEditing = () => {
    setSelectedMode(workspace.execution_mode);
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
          {overview.stores.map((store) => (
            <li key={store.id}>
              <span>{store.name}</span>
              <span>{store.platform}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Agent">
        <h2>Agent</h2>
        {overview.agent ? (
          <ul>
            <li>
              <span>{overview.agent.name}</span>
              <span>v{overview.agent.version}</span>
            </li>
          </ul>
        ) : (
          <p>当前 Workspace 未启用 Agent</p>
        )}
      </section>

      <section aria-label="Model">
        <h2>Model</h2>
        <ul>
          {overview.model_connections.map((model) => (
            <li key={model.id}>
              <span>{model.name}</span>
              <span>{model.backend_type}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Skill">
        <h2>Skill</h2>
        <ul>
          {overview.skills.map((skill) => (
            <li key={skill.id}>
              <span>{skill.name}</span>
              <span>{skill.latest_version === null ? "未发布" : `v${skill.latest_version}`}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Source">
        <h2>Source</h2>
        <ul>
          {overview.sources.map((source) => (
            <li key={source.id}>
              <span>{source.name}</span>
              <span>{source.kind}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="业务规则">
        <h2>业务规则</h2>
        <ul>
          {overview.business_policies.map((policy) => (
            <li key={policy.id}>
              <span>{policy.name}</span>
              <span>{policy.latest_version === null ? "未发布" : `v${policy.latest_version}`}</span>
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
            <p>当前执行模式:{executionModeLabels[workspace.execution_mode]}</p>
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

      <section aria-label="当前成员权限">
        <h2>当前成员权限</h2>
        <p>角色:{overview.current_membership.role}</p>
        <p>动作权限:{overview.current_membership.permissions.join("、") || "无"}</p>
        <ul>
          {Object.entries(overview.current_membership.resource_scopes).map(([type, scope]) => (
            <li key={type}>
              {type}:{scope.mode === "ALL" ? "全部" : scope.resource_ids.join("、") || "无"}
            </li>
          ))}
        </ul>
      </section>
    </section>
  );
}
