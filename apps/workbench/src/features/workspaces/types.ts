export type WorkspaceRole = "OWNER" | "OPERATOR";

export type ExecutionMode = "AUTO_EXECUTE" | "AWAIT_CONFIRMATION";

export interface StoreView {
  id: string;
  name: string;
  platform: string;
}

export interface AgentView {
  name: string;
  agentVersion: string;
}

export interface ModelView {
  name: string;
  provider: string;
}

export interface SkillView {
  name: string;
  skillVersion: string;
}

export interface SourceView {
  name: string;
  kind: "LOCAL" | "MCP";
}

export interface MemberView {
  id: string;
  displayName: string;
  role: WorkspaceRole;
  resourceScopes: string[];
}

export interface WorkspaceDetailView {
  id: string;
  name: string;
  executionMode: ExecutionMode;
  stores: StoreView[];
  agents: AgentView[];
  models: ModelView[];
  skills: SkillView[];
  sources: SourceView[];
  members: MemberView[];
}
