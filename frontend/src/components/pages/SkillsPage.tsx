import { SkillManager } from '../SkillManager';

/** 技能页：行为契约能力管理（与设置弹窗共用 SkillManager） */
export function SkillsPage() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-shrink-0 items-center justify-between border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">技能管理</span>
        <span className="rounded-full border border-line bg-black/[0.02] px-2.5 py-1 text-[11px] text-ink-3">
          渐进式加载 · 对话先 load_skill 再使用工具
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[860px] animate-fade-up px-6 py-5">
          <SkillManager />
        </div>
      </div>
    </div>
  );
}