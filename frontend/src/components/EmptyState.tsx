import { TOOL_META } from '../lib/format';
import BlurText from './reactbits/BlurText';

/** 空状态快捷指令：点击直接发送（对应后端四类真实工具） */
const QUICK_PROMPTS: { tool: string; label: string; desc: string; prompt: string }[] = [
  {
    tool: 'query_inventory',
    label: '查询库存',
    desc: '淘宝防晒霜 SKU-001 还有多少货',
    prompt: '查询淘宝渠道 SKU-001 防晒霜的库存',
  },
  {
    tool: 'update_price',
    label: '调整价格',
    desc: '把京东无线耳机的价格调到 299 元',
    prompt: '把京东渠道 SKU-002 无线蓝牙耳机的价格调整为 299 元',
  },
  {
    tool: 'create_promotion',
    label: '创建促销',
    desc: '在抖音上线一个限时 8 折活动',
    prompt: '在抖音渠道为 SKU-003 创建一个限时 8 折的促销活动',
  },
  {
    tool: 'query_order_status',
    label: '跟踪订单',
    desc: '查询订单 TB-10086 的最新状态',
    prompt: '查询淘宝渠道订单 TB-10086 的状态',
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
          已接入淘宝、京东、抖音三个渠道。用一句话就能查库存、调价格、创建促销、跟踪订单，我来执行并汇报结果。
        </p>

        <div className="mt-7 grid grid-cols-2 gap-3 text-left">
          {QUICK_PROMPTS.map((p, i) => (
            <button
              key={p.tool}
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

        <p className="mt-6 text-[11.5px] text-ink-3">输入运营指令开始 · 例如：查询淘宝防晒霜 SKU-001 的库存</p>
      </div>
    </div>
  );
}
