export type WorkspaceRole = "WORKSPACE_OWNER" | "OPERATOR";

export type ExecutionMode = "READ_ONLY" | "CONFIRM_WRITES" | "AUTO_EXECUTE";

export interface WorkspaceOverviewAgent {
  id: string;
  stable_key: string;
  name: string;
  version: number;
  status: string;
}

export interface WorkspaceOverview {
  workspace: {
    id: string;
    name: string;
    status: string;
    execution_mode: ExecutionMode;
    state_version: number;
  };
  current_membership: {
    id: string;
    role: WorkspaceRole;
    permissions: string[];
    resource_scopes: Record<string, { mode: string; resource_ids: string[] }>;
  };
  stores: Array<{ id: string; name: string; platform: string; status: string }>;
  agent: WorkspaceOverviewAgent | null;
  model_connections: Array<{
    id: string;
    stable_key: string;
    name: string;
    backend_type: string;
    base_url: string;
    masked_key: string | null;
    model: string;
    version: number;
    updated_at: string | null;
  }>;
  skills: Array<{
    id: string;
    stable_key: string;
    name: string;
    latest_version: number | null;
    status: string | null;
  }>;
  sources: Array<{
    id: string;
    stable_key: string;
    kind: string;
    name: string;
    latest_version: number | null;
    status: string | null;
  }>;
  business_policies: Array<{
    id: string;
    stable_key: string;
    name: string;
    latest_version: number | null;
    status: string | null;
  }>;
}
