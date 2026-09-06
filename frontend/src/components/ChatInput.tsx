import { useCallback, useEffect, useRef, useState } from 'react';
import type { ModelProvider, PermissionModeType, Skill } from '../types';

/** 权限模式：对应后端 PermissionMode（Codex 风格，放在输入框左下角） */
const MODES: { id: PermissionModeType; label: string; dot: string; desc: string }[] = [
  { id: 'READONLY', label: '只读模式', dot: 'bg-ok', desc: '只读操作自动执行，写操作自动拦截' },
  { id: 'ASK', label: '询问模式', dot: 'bg-warn', desc: '写操作执行前需要确认' },
  { id: 'EXECUTE', label: '执行模式', dot: 'bg-danger', desc: '所有操作自动执行（高风险）' },
];

const PROVIDER_DOT: Record<string, string> = { openai: 'bg-accent', anthropic: 'bg-amber-500', mock: 'bg-ink-3/50' };

const THINKING_OPTIONS: { id: string; label: string }[] = [
  { id: 'low', label: '思考：低' },
  { id: 'high', label: '思考：高' },
  { id: 'highest', label: '思考：最高' },
];

interface ChatInputProps {
  onSend: (text: string) => void;
  onAbort: () => void;
  isStreaming: boolean;
  disabled: boolean;
  mode: PermissionModeType;
  providers: ModelProvider[];
  activeProvider: string;
  onActivateProvider: (name: string) => void;
  onModeChange: (mode: PermissionModeType) => void;
  thinkingLevel: string;
  onThinkingLevelChange: (level: string) => void;
  /** 技能菜单：输入 "/" 时弹出补全，选中即 "/技能名 " 前缀（引擎侧显式预加载） */
  skills: Skill[];
}

/** 底部输入区：Enter 发送 / Shift+Enter 换行 / Esc 中断，自动增高；左下角内嵌模型与权限模式切换 */
export function ChatInput({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  mode,
  providers,
  activeProvider,
  onActivateProvider,
  onModeChange,
  thinkingLevel,
  onThinkingLevelChange,
  skills,
}: ChatInputProps) {
  const [value, setValue] = useState('');
  const [modeOpen, setModeOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const modelPickerRef = useRef<HTMLDivElement>(null);
  const thinkingRef = useRef<HTMLDivElement>(null);

  /* / 命令补全：首个 token 形如 "/xxx"（未输入空格）时弹出技能菜单 */
  const cmdMatch = value.match(/^\/([A-Za-z0-9_]*)$/);
  const slashQuery = cmdMatch && !slashDismissed ? cmdMatch[1].toLowerCase() : null;
  const slashMatches = slashQuery === null
    ? []
    : skills.filter((s) => s.enabled && s.name.toLowerCase().startsWith(slashQuery));
  const slashOpen = slashQuery !== null && slashMatches.length > 0;

  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, []);

  useEffect(() => {
    resize();
  }, [value, resize]);

  useEffect(() => {
    if (!isStreaming) ref.current?.focus();
  }, [isStreaming]);

  /* 点击外部关闭下拉 */
  useEffect(() => {
    if (!modeOpen && !modelOpen && !thinkingOpen) return;
    const onDown = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setModeOpen(false);
      if (modelPickerRef.current && !modelPickerRef.current.contains(e.target as Node)) setModelOpen(false);
      if (thinkingRef.current && !thinkingRef.current.contains(e.target as Node)) setThinkingOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [modeOpen, modelOpen, thinkingOpen]);

  const current = MODES.find((m) => m.id === mode) ?? MODES[1];
  const currentProvider = providers.find((p) => p.name === activeProvider);

  useEffect(() => {
    setSlashIndex(0);
  }, [slashQuery]);

  const pickSkill = (name: string) => {
    setValue(`/${name} `);
    setSlashDismissed(false);
    ref.current?.focus();
  };

  const send = () => {
    const t = value.trim();
    if (!t || disabled || isStreaming) return;
    onSend(t);
    setValue('');
    setSlashDismissed(false);
  };

  return (
    <div className="flex-shrink-0 px-6 pb-4">
      <div className="mx-auto max-w-[1060px]">
        <div className="relative">
          {/* / 命令技能菜单：输入 / 弹出，↑↓ 选择，Tab/Enter 补全，Esc 关闭 */}
          {slashOpen && (
            <div className="animate-fade-up absolute bottom-full left-0 right-0 z-20 mb-2 rounded-xl border border-line bg-elevated p-1 shadow-pop">
              <div className="px-2.5 pb-1 pt-1.5 text-[10.5px] text-ink-3">
                / 技能直达 · ↑↓ 选择，Tab 补全后接指令，Esc 关闭
              </div>
              {slashMatches.map((s, i) => (
                <button
                  key={s.name}
                  onClick={() => pickSkill(s.name)}
                  onMouseEnter={() => setSlashIndex(i)}
                  className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors ${
                    i === slashIndex ? 'bg-accent-soft' : 'hover:bg-black/[0.04]'
                  }`}
                >
                  <span className="font-mono text-[12px] font-medium text-ink">/{s.name}</span>
                  <span className="min-w-0 flex-1 truncate text-[11.5px] text-ink-2">{s.description}</span>
                  {s.builtin && <span className="rounded bg-inset px-1.5 py-px text-[10px] text-ink-3">内置</span>}
                </button>
              ))}
            </div>
          )}
          <div className="rounded-xl bg-elevated px-4 py-3 shadow-card transition-all focus-within:[box-shadow:var(--shadow-input-focus)]">
          <textarea
            ref={ref}
            rows={1}
            value={value}
            disabled={disabled || isStreaming}
            onChange={(e) => {
              setValue(e.target.value);
              setSlashDismissed(false);
            }}
            onKeyDown={(e) => {
              if (slashOpen) {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                  e.preventDefault();
                  setSlashIndex((i) =>
                    (i + (e.key === 'ArrowDown' ? 1 : slashMatches.length - 1)) % slashMatches.length,
                  );
                  return;
                }
                if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing)) {
                  e.preventDefault();
                  pickSkill(slashMatches[slashIndex].name);
                  return;
                }
                if (e.key === 'Escape') {
                  e.preventDefault();
                  setSlashDismissed(true);
                  return;
                }
              }
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
              if (e.key === 'Escape' && isStreaming) {
                e.preventDefault();
                onAbort();
              }
            }}
            placeholder={disabled ? '未连接到服务器，请确认后端已启动…' : '输入运营指令，回车发送；输入 / 直达技能…'}
            className="block max-h-[168px] w-full resize-none bg-transparent text-[14px] leading-relaxed text-ink placeholder:text-ink-3 focus:outline-none disabled:opacity-60"
          />
          <div className="mt-1.5 flex items-center justify-between gap-3">
            <div className="flex items-center gap-1.5">
              {/* 模型切换（Codex 风格：composer 左下角 chip + 向上弹层） */}
              <div ref={modelPickerRef} className="relative">
                <button
                  onClick={() => setModelOpen((v) => !v)}
                  disabled={isStreaming}
                  title="切换模型供应商"
                  className="flex h-7 items-center gap-1.5 rounded-lg border border-line px-2 text-[11.5px] text-ink-2 transition-colors hover:bg-black/[0.03] disabled:opacity-50"
                >
                  <svg className="h-3.5 w-3.5 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="5" y="5" width="14" height="14" rx="2" />
                    <rect x="9" y="9" width="6" height="6" />
                    <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
                  </svg>
                  <span className="max-w-[120px] truncate">{currentProvider?.label ?? activeProvider}</span>
                  <svg
                    className={`h-3 w-3 text-ink-3 transition-transform ${modelOpen ? 'rotate-180' : ''}`}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </button>

                {modelOpen && (
                  <div className="animate-fade-up absolute bottom-full left-0 z-20 mb-2 w-[300px] rounded-xl border border-line bg-elevated p-1 shadow-pop">
                    {providers.length === 0 && (
                      <div className="px-3 py-3 text-center text-[12px] text-ink-3">暂无供应商，可在「设置 → 模型配置」新增</div>
                    )}
                    {providers.map((p) => (
                      <button
                        key={p.name}
                        onClick={() => {
                          onActivateProvider(p.name);
                          setModelOpen(false);
                        }}
                        className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors ${
                          p.name === activeProvider ? 'bg-accent-soft' : 'hover:bg-black/[0.04]'
                        }`}
                      >
                        <span className={`mt-[6px] h-2 w-2 flex-shrink-0 rounded-full ${PROVIDER_DOT[p.provider] ?? 'bg-ink-3/50'}`} />
                        <span className="min-w-0">
                          <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-ink">
                            {p.label}
                            {p.name === activeProvider && <span className="text-[10.5px] font-normal text-accent">当前</span>}
                          </span>
                          <span className="mt-0.5 block truncate font-mono text-[11px] text-ink-3">
                            {p.provider} · {p.default_model || '-'}
                            {p.thinking_level ? ` · 思考:${p.thinking_level}` : ''}
                          </span>
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* 权限模式（Codex 风格：composer 左下角 chip + 向上弹层） */}
              <div ref={pickerRef} className="relative">
                <button
                  onClick={() => setModeOpen((v) => !v)}
                  disabled={isStreaming}
                  title={current.desc}
                  className="flex h-7 items-center gap-1.5 rounded-lg border border-line px-2 text-[11.5px] text-ink-2 transition-colors hover:bg-black/[0.03] disabled:opacity-50"
                >
                  <span className={`h-1.5 w-1.5 rounded-full ${current.dot}`} />
                  {current.label}
                  <svg
                    className={`h-3 w-3 text-ink-3 transition-transform ${modeOpen ? 'rotate-180' : ''}`}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </button>

                {modeOpen && (
                  <div className="animate-fade-up absolute bottom-full left-0 z-20 mb-2 w-[300px] rounded-xl border border-line bg-elevated p-1 shadow-pop">
                    {MODES.map((m) => (
                      <button
                        key={m.id}
                        onClick={() => {
                          onModeChange(m.id);
                          setModeOpen(false);
                        }}
                        className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors ${
                          m.id === mode ? 'bg-accent-soft' : 'hover:bg-black/[0.04]'
                        }`}
                      >
                        <span className={`mt-[5px] h-2 w-2 flex-shrink-0 rounded-full ${m.dot}`} />
                        <span className="min-w-0">
                          <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-ink">
                            {m.label}
                            {m.id === mode && <span className="text-[10.5px] font-normal text-accent">当前</span>}
                          </span>
                          <span className="mt-0.5 block text-[11px] leading-relaxed text-ink-3">{m.desc}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* 思考强度（Codex 风格：composer 左下角 chip + 向上弹层） */}
              <div ref={thinkingRef} className="relative">
                <button
                  onClick={() => setThinkingOpen((v) => !v)}
                  disabled={isStreaming}
                  title="切换思考强度"
                  className="flex h-7 items-center gap-1.5 rounded-lg border border-line px-2 text-[11.5px] text-ink-2 transition-colors hover:bg-black/[0.03] disabled:opacity-50"
                >
                  <svg className="h-3.5 w-3.5 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                  <span>{THINKING_OPTIONS.find((o) => o.id === thinkingLevel)?.label ?? '思考强度'}</span>
                  <svg
                    className={`h-3 w-3 text-ink-3 transition-transform ${thinkingOpen ? 'rotate-180' : ''}`}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </button>

                {thinkingOpen && (
                  <div className="animate-fade-up absolute bottom-full left-0 z-20 mb-2 w-[200px] rounded-xl border border-line bg-elevated p-1 shadow-pop">
                    {THINKING_OPTIONS.map((opt) => (
                      <button
                        key={opt.id}
                        onClick={() => {
                          onThinkingLevelChange(opt.id);
                          setThinkingOpen(false);
                        }}
                        className={`flex w-full items-center rounded-lg px-2.5 py-2 text-left text-[12.5px] transition-colors ${
                          opt.id === thinkingLevel ? 'bg-accent-soft font-medium text-accent' : 'text-ink hover:bg-black/[0.04]'
                        }`}
                      >
                        {opt.label}
                        {opt.id === thinkingLevel && <span className="ml-auto text-[10.5px] font-normal text-accent">当前</span>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {isStreaming && (
              <span className="hidden text-[11px] text-ink-3 sm:block">Esc 中断生成</span>
            )}

            {isStreaming ? (
              <button
                onClick={onAbort}
                className="flex h-8 flex-shrink-0 items-center gap-1.5 rounded-lg bg-[#FDEDED] px-3 text-[12.5px] font-medium text-danger transition-colors hover:bg-[#F9DCDD]"
              >
                <span className="h-2 w-2 rounded-[2px] bg-danger" />
                停止
              </button>
            ) : (
              <button
                onClick={send}
                disabled={disabled || !value.trim()}
                className="flex h-8 flex-shrink-0 items-center gap-1 rounded-lg bg-accent px-3.5 text-[12.5px] font-medium text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
              >
                发送
                <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="19" x2="12" y2="5" />
                  <polyline points="5 12 12 5 19 12" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
      </div>
    </div>
  );
}