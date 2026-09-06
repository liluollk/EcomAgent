import { useCallback, useEffect, useState } from 'react';
import type { Skill } from '../../types';

/** 技能页 — 只读查看器：技能即知识包（SOP / 平台规则），展示菜单与正文。
 *
 * 创建不走表单：在对话里让 Agent 用 skill_creator 沉淀（"把刚才的操作做成技能"），
 * save_skill 经 HITL 落盘并热加载进 load_skill 菜单——管理界面只读，避免与
 * Agent 化的创建路径形成两套事实源。
 */
export function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [openName, setOpenName] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const resp = await fetch('/skills');
      const list = (await resp.json()) as Skill[];
      setSkills(Array.isArray(list) ? list : []);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-shrink-0 items-center border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">技能菜单</span>
        <span className="ml-2 rounded-full bg-inset px-2 py-0.5 text-[11px] text-ink-3">
          {skills.length} 个 · 渐进式加载
        </span>
        <span className="ml-auto text-[11.5px] text-ink-3">
          新增技能在对话中进行：让 Agent「把刚才的操作做成技能」（skill_creator）
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[860px] animate-fade-up space-y-2.5 px-6 py-5">
          {failed && (
            <div className="rounded-xl bg-elevated px-5 py-8 text-center text-[12.5px] text-ink-3 shadow-card">
              无法加载技能列表，请确认后端已启动
            </div>
          )}
          {!failed && skills.length === 0 && (
            <div className="rounded-xl bg-elevated px-5 py-8 text-center text-[12.5px] text-ink-3 shadow-card">
              暂无技能——在对话中让 Agent 沉淀第一个技能
            </div>
          )}
          {skills.map((s) => {
            const open = openName === s.name;
            return (
              <div
                key={s.name}
                className="overflow-hidden rounded-xl bg-elevated shadow-card"
              >
                <button
                  onClick={() => setOpenName(open ? null : s.name)}
                  className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-black/[0.02]"
                >
                  <span className="font-mono text-[12.5px] font-medium text-ink">{s.name}</span>
                  {s.builtin && (
                    <span className="rounded bg-inset px-1.5 py-px text-[10.5px] text-ink-3">内置</span>
                  )}
                  {!s.enabled && (
                    <span className="rounded bg-[#F7E8B8] px-1.5 py-px text-[10.5px] text-[#92610A]">已停用</span>
                  )}
                  <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">{s.description}</span>
                  <svg
                    className={`h-3.5 w-3.5 flex-shrink-0 text-ink-3 transition-transform ${open ? 'rotate-90' : ''}`}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                </button>

                {open && (
                  <div className="border-t border-line px-4 py-3">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {s.keywords.map((k) => (
                        <span key={k} className="rounded bg-accent-soft px-1.5 py-px text-[10.5px] text-accent">
                          {k}
                        </span>
                      ))}
                      {s.prerequisites.map((p) => (
                        <span key={p} className="rounded bg-[#F7E8B8] px-1.5 py-px font-mono text-[10.5px] text-[#92610A]">
                          前置: {p}
                        </span>
                      ))}
                    </div>
                    <div className="mb-1 mt-2.5 text-[11px] font-medium text-ink-3">SOP 正文</div>
                    <pre className="max-h-[280px] overflow-auto whitespace-pre-wrap rounded-lg bg-inset px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-ink-2">
                      {s.body || '（无正文，按描述执行）'}
                    </pre>
                    <div className="mt-2 text-[11px] text-ink-3">
                      对话中输入 <span className="font-mono text-ink-2">/{s.name}</span> 可在发送指令时直达该技能
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
