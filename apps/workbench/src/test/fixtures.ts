import type { WorkspaceOverview } from "../features/workspaces/types";

export const workspaceOverview: WorkspaceOverview = {
  workspace: {
    id: "ws-brand-1",
    name: "品牌一",
    status: "ACTIVE",
    execution_mode: "CONFIRM_WRITES",
    state_version: 1,
  },
  current_membership: {
    id: "membership-owner",
    role: "WORKSPACE_OWNER",
    permissions: ["product.read", "product.price.write"],
    resource_scopes: {
      store: { mode: "SELECTED", resource_ids: ["store-tb"] },
    },
  },
  stores: [
    { id: "store-tb", name: "淘宝旗舰店", platform: "TAOBAO", status: "ACTIVE" },
    { id: "store-dy", name: "抖音小店", platform: "DOUYIN", status: "ACTIVE" },
  ],
  agent: {
    id: "agent-product-ops",
    stable_key: "product-operations",
    name: "商品运营 Agent",
    version: 1,
    status: "PUBLISHED",
  },
  model_connections: [
    {
      id: "model-1",
      stable_key: "deepseek",
      name: "deepseek-chat",
      backend_type: "OPENAI",
      base_url: "https://model.invalid/v1",
      masked_key: "****",
      model: "deepseek-chat",
      version: 1,
      updated_at: null,
    },
  ],
  skills: [
    { id: "skill-1", stable_key: "price-ops", name: "改价与上架", latest_version: 3, status: "PUBLISHED" },
  ],
  sources: [
    { id: "source-1", stable_key: "wiki", kind: "MCP", name: "运营知识库", latest_version: 1, status: "PUBLISHED" },
  ],
  business_policies: [
    { id: "policy-1", stable_key: "price-floor", name: "最低价规则", latest_version: 1, status: "PUBLISHED" },
  ],
};
