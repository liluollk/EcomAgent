import { useState } from "react";
import { WorkspaceContainer } from "../features/workspaces/WorkspaceContainer";

type FeatureKey = "workspace" | "sessions" | "settings";

const sidebarFeatures: Array<{ key: FeatureKey; label: string }> = [
  { key: "workspace", label: "Workspace" },
  { key: "sessions", label: "Sessions" },
  { key: "settings", label: "Settings" },
];

export function App() {
  const [activeFeature, setActiveFeature] = useState<FeatureKey>("workspace");

  return (
    <div className="app-shell">
      <nav aria-label="功能导航">
        {sidebarFeatures.map((feature) => (
          <button
            key={feature.key}
            type="button"
            onClick={() => setActiveFeature(feature.key)}
            aria-current={activeFeature === feature.key ? "true" : undefined}
          >
            {feature.label}
          </button>
        ))}
      </nav>
      <main>
        {activeFeature === "workspace" && (
          // TODO(Task 10+): currentRole 与 workspaceId 由登录态 Membership 提供;
          // 不建立全局单店上下文,Workspace 内多店并列展示。
          <WorkspaceContainer currentRole="OPERATOR" workspaceId="ws-brand-1" />
        )}
        {activeFeature === "sessions" && <p>Sessions 页面将在后续任务实现。</p>}
        {activeFeature === "settings" && <p>Settings 页面将在后续任务实现。</p>}
      </main>
    </div>
  );
}
