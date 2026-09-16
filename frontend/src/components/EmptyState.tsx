import { TOOL_META } from '../lib/format';
import BlurText from './reactbits/BlurText';

/** 空状态快捷指令：点击直接发送（对应默认调价闭环的模型可见工具）
 *
 * 只放主流程：查快照 → 提交目标价（含两个平台各一条）→ 沉淀技能。
 * 扩展工具（工单 / 知识库 / 经营分析）不在默认注册表里，不作为入口示例。
 */
const QUICK_PROMPTS: { tool: string; label: string; desc: string; prompt: string }[] = [
  {
    tool: 'query_product_snapshot',
    label: '查商品快照',
    desc: '淘宝 ITEM-1001/SKU-002 现在什么价、还有多少货',
    prompt: '查一下淘宝 ITEM-1001/SKU-002 的当前价和库存',
  },
  {
    tool: 'update_price',
    label: '淘宝调价',
    desc: '把淘宝 ITEM-1001/SKU-002 的价格调到 99 元',
    prompt: '把淘宝 ITEM-1001/SKU-002 的价格调到 99',
  },
  {
    tool: 'update_price',
    label: '抖店调价',
    desc: '把抖店 ITEM-1001/SKU-002 的价格调到 95 元',
    prompt: '把抖店 ITEM-1001/SKU-002 的价格调到 95',
  },
  {
    tool: 'save_skill',
    label: '沉淀技能',
    desc: '把刚才的调价流程做成一个技能',
    prompt: '把刚才的调价流程做成一个叫 price_flow 的技能',
  },
];

export function EmptyState({ onPrompt }: { onPrompt: (text: string) => void }) {
  return (
    <div className="flex flex-1 items-center justify-center overflow-y-auto px-6">
      <div className="w-full max-w-[560px] animate-fade-up pb-10 text-center">
        <h2 className="sr-only">你好，我是你的电商运营助手</h2>
        <BlurText
          text="你好，我是你的电商运营助手"
          animateBy="letters"
          delay={35}
          className="mt-0 justify-center text-[20px] font-semibold tracking-tight text-ink"
        />
        <p className="mx-auto mt-2 max-w-[420px] text-[13px] leading-relaxed text-ink-2">
          支持淘宝、抖店两个平台的商品调价：先查快照，再提交目标价，经审批后执行，最后以平台回查确认结果。
        </p>

        <div className="mt-7 grid grid-cols-2 gap-3 text-left">
          {QUICK_PROMPTS.map((p, i) => (
            <button
              key={p.prompt}
              onClick={() => onPrompt(p.prompt)}
              style={{ animationDelay: `${120 + i * 60}ms` }}
              className="stagger-item group rounded-xl bg-elevated p-3.5 text-left shadow-card transition-all hover:shadow-pop"
            >
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent-soft text-accent">
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d={TOOL_META[p.tool]?.icon} />
                  </svg>
                </span>
                <span className="text-[13px] font-medium text-ink">{p.label}</span>
              </div>
              <p className="mt-1.5 text-[12px] leading-relaxed text-ink-3">{p.desc}</p>
            </button>
          ))}
        </div>

        <p className="mt-6 text-[11.5px] text-ink-3">输入运营指令开始 · 例如：把淘宝 ITEM-1001/SKU-002 的价格调到 99</p>
      </div>
    </div>
  );
}
