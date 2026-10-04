import type { WorkspaceDetailView } from "../features/workspaces/types";

export const workspaceDetail: WorkspaceDetailView = {
  id: "ws-brand-1",
  name: "品牌一",
  executionMode: "AWAIT_CONFIRMATION",
  stores: [
    { id: "store-tb", name: "淘宝旗舰店", platform: "淘宝" },
    { id: "store-dy", name: "抖音小店", platform: "抖音" },
  ],
  agents: [{ name: "商品运营 Agent", agentVersion: "v1.2.0" }],
  models: [{ name: "deepseek-chat", provider: "DeepSeek" }],
  skills: [{ name: "改价与上架", skillVersion: "v3" }],
  sources: [{ name: "weknora-knowledge", kind: "MCP" }],
  members: [
    { id: "m-1", displayName: "张三", role: "OWNER", resourceScopes: ["全部店铺"] },
    { id: "m-2", displayName: "李四", role: "OPERATOR", resourceScopes: ["淘宝旗舰店"] },
  ],
};
