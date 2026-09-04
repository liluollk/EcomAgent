import { useCallback, useState } from 'react';
import { CHANNEL_META, CHANNEL_LIST } from '../../lib/format';
import CountUp from '../reactbits/CountUp';
import {
  CHANNEL_SUMMARIES,
  INITIAL_TASKS,
  PRODUCTS,
  PROMOTIONS,
  type Product,
  type Task,
} from './data';

type WorkspaceTab = 'overview' | 'products' | 'promotions' | 'tasks';

const TABS: { id: WorkspaceTab; label: string }[] = [
  { id: 'overview', label: '总览' },
  { id: 'products', label: '商品' },
  { id: 'promotions', label: '促销' },
  { id: 'tasks', label: '任务' },
];

/** 工作台：总览 / 商品 / 促销 / 任务 四个分区（当前为演示数据） */
export function WorkspacePage() {
  const [tab, setTab] = useState<WorkspaceTab>('overview');

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 分区导航 */}
      <div className="flex flex-shrink-0 items-center justify-between border-b border-line px-5 py-2.5">
        <div className="flex items-center gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded-md px-3 py-1.5 text-[13px] transition-colors ${
                tab === t.id ? 'bg-accent-soft font-medium text-accent' : 'text-ink-2 hover:bg-black/[0.04] hover:text-ink'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="rounded-full border border-[#F0D48A] bg-[#FFFBEB] px-2.5 py-1 text-[11px] font-medium text-[#92610A]">
          演示数据 · 接入真实渠道后自动更新
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div key={tab} className="mx-auto max-w-[980px] animate-fade-up px-6 py-5">
          {tab === 'overview' && <OverviewTab />}
          {tab === 'products' && <ProductsTab />}
          {tab === 'promotions' && <PromotionsTab />}
          {tab === 'tasks' && <TasksTab />}
        </div>
      </div>
    </div>
  );
}

/* ============================== 总览 ============================== */

function OverviewTab() {
  const totalProducts = CHANNEL_SUMMARIES.reduce((s, c) => s + c.products, 0);
  const totalAlerts = CHANNEL_SUMMARIES.reduce((s, c) => s + c.alerts, 0);
  const totalSales = CHANNEL_SUMMARIES.reduce((s, c) => s + c.todaySales, 0);
  const activePromos = PROMOTIONS.filter((p) => p.status === 'active').length;

  return (
    <div className="space-y-5 animate-fade-up">
      <div className="grid grid-cols-4 gap-4">
        <KpiCard label="在售商品" value={totalProducts} />
        <KpiCard label="库存预警" value={totalAlerts} tone="warn" />
        <KpiCard label="今日销量" value={totalSales} tone="ok" />
        <KpiCard label="进行中促销" value={activePromos} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {CHANNEL_SUMMARIES.map((c) => {
          const meta = CHANNEL_META[c.id];
          return (
            <div key={c.id} className="overflow-hidden rounded-xl border border-line bg-elevated shadow-card">
              <div className="flex items-center gap-2.5 border-b border-line px-4 py-3">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: meta.color }} />
                <span className="text-[13.5px] font-semibold text-ink">{meta.label}</span>
                <span className="ml-auto text-[11.5px] text-ink-3">{c.products} 件商品</span>
              </div>
              <div className="grid grid-cols-3 gap-2 px-4 py-3">
                <MiniStat label="商品数" value={c.products} />
                <MiniStat label="库存预警" value={c.alerts} tone={c.alerts > 3 ? 'danger' : 'warn'} />
                <MiniStat label="今日销量" value={c.todaySales} tone="ok" />
              </div>
              <div className="border-t border-line">
                <div className="px-4 pb-1 pt-2.5 text-[11px] font-medium text-ink-3">TOP 商品</div>
                {c.top.map((p) => (
                  <div key={p.id} className="flex items-center justify-between gap-2 px-4 py-2 text-[12px]">
                    <div className="min-w-0">
                      <div className="truncate text-ink">{p.name}</div>
                      <div className="font-mono text-[10.5px] text-ink-3">{p.id}</div>
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-3 text-ink-2">
                      <span>¥{p.price}</span>
                      <span className={p.stock <= 10 ? 'font-medium text-danger' : ''}>库存 {p.stock}</span>
                      <span>售 {p.sales}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function KpiCard({ label, value, tone }: { label: string; value: number; tone?: 'ok' | 'warn' }) {
  return (
    <div className="rounded-xl border border-line bg-elevated p-4 shadow-card">
      <div className="text-[12px] text-ink-3">{label}</div>
      <div
        className={`mt-1 text-[24px] font-bold leading-none tracking-tight ${
          tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : 'text-ink'
        }`}
      >
        <CountUp to={value} duration={1.2} />
      </div>
    </div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: number; tone?: 'ok' | 'warn' | 'danger' }) {
  const color = tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : tone === 'danger' ? 'text-danger' : 'text-ink';
  return (
    <div>
      <div className="text-[10.5px] text-ink-3">{label}</div>
      <div className={`text-[15px] font-semibold ${color}`}>{value}</div>
    </div>
  );
}

/* ============================== 商品 ============================== */

function ProductsTab() {
  const [query, setQuery] = useState('');
  const [channel, setChannel] = useState<'all' | string>('all');
  const [status, setStatus] = useState<'all' | Product['status']>('all');

  const filtered = PRODUCTS.filter((p) => channel === 'all' || p.channel === channel)
    .filter((p) => status === 'all' || p.status === status)
    .filter((p) => p.name.toLowerCase().includes(query.toLowerCase()) || p.id.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="space-y-4 animate-fade-up">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <svg className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索商品名称 / SKU"
            className="h-9 w-56 rounded-lg border border-line bg-elevated pl-8 pr-3 text-[13px] text-ink placeholder:text-ink-3 focus:border-accent/50 focus:outline-none"
          />
        </div>
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          className="h-9 rounded-lg border border-line bg-elevated px-3 text-[13px] text-ink focus:border-accent/50 focus:outline-none"
        >
          <option value="all">全部渠道</option>
          {CHANNEL_LIST.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as typeof status)}
          className="h-9 rounded-lg border border-line bg-elevated px-3 text-[13px] text-ink focus:border-accent/50 focus:outline-none"
        >
          <option value="all">全部状态</option>
          <option value="normal">正常</option>
          <option value="low_stock">库存偏低</option>
          <option value="out_of_stock">缺货</option>
        </select>
        <span className="ml-auto text-[12px] text-ink-3">{filtered.length} 件商品</span>
      </div>

      <div className="overflow-hidden rounded-xl border border-line bg-elevated shadow-card">
        {filtered.map((p, idx) => (
          <div
            key={p.id}
            className={`flex items-center gap-3 px-4 py-3 ${idx > 0 ? 'border-t border-line' : ''} transition-colors hover:bg-black/[0.015]`}
          >
            <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-inset text-[18px]">{p.image}</div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13.5px] font-medium text-ink">{p.name}</div>
              <div className="mt-0.5 flex items-center gap-2">
                <ChannelTag id={p.channel} />
                <span className="font-mono text-[11px] text-ink-3">{p.id}</span>
              </div>
            </div>
            <Metric label="价格" value={`¥${p.price}`} />
            <Metric
              label="库存"
              value={String(p.stock)}
              tone={p.stock === 0 ? 'danger' : p.stock < 50 ? 'warn' : undefined}
            />
            <Metric label="销量" value={String(p.sales)} />
            <StockBadge status={p.status} />
          </div>
        ))}
        {filtered.length === 0 && <div className="px-4 py-12 text-center text-[13px] text-ink-3">没有匹配的商品，调整筛选条件试试</div>}
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: 'warn' | 'danger' }) {
  const color = tone === 'warn' ? 'text-warn' : tone === 'danger' ? 'text-danger' : 'text-ink';
  return (
    <div className="w-16 flex-shrink-0 text-right">
      <div className="text-[10.5px] text-ink-3">{label}</div>
      <div className={`text-[13.5px] font-medium ${color}`}>{value}</div>
    </div>
  );
}

function StockBadge({ status }: { status: Product['status'] }) {
  if (status === 'normal') {
    return <span className="w-16 flex-shrink-0 text-right text-[11.5px] font-medium text-ok">正常</span>;
  }
  if (status === 'low_stock') {
    return <span className="w-16 flex-shrink-0 text-right text-[11.5px] font-medium text-warn">库存偏低</span>;
  }
  return <span className="w-16 flex-shrink-0 text-right text-[11.5px] font-medium text-danger">缺货</span>;
}

function ChannelTag({ id }: { id: keyof typeof CHANNEL_META }) {
  const meta = CHANNEL_META[id];
  return (
    <span
      className="rounded px-1.5 py-px text-[10.5px] font-medium"
      style={{ backgroundColor: `${meta.color}14`, color: meta.color }}
    >
      {meta.label}
    </span>
  );
}

/* ============================== 促销 ============================== */

function PromotionsTab() {
  const [channel, setChannel] = useState<'all' | string>('all');
  const filtered = PROMOTIONS.filter((p) => channel === 'all' || p.channel === channel);

  return (
    <div className="space-y-4 animate-fade-up">
      <div className="flex items-center gap-2">
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          className="h-9 rounded-lg border border-line bg-elevated px-3 text-[13px] text-ink focus:border-accent/50 focus:outline-none"
        >
          <option value="all">全部渠道</option>
          {CHANNEL_LIST.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label}
            </option>
          ))}
        </select>
        <span className="ml-auto text-[12px] text-ink-3">{filtered.length} 个活动</span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {filtered.map((promo) => {
          const progress = promo.target === 0 ? 0 : Math.min((promo.sales / promo.target) * 100, 100);
          const barColor = progress >= 80 ? 'var(--success)' : progress >= 40 ? 'var(--warning)' : 'var(--danger)';
          return (
            <div key={promo.id} className="rounded-xl border border-line bg-elevated p-4 shadow-card">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="text-[14px] font-semibold text-ink">{promo.name}</div>
                  <div className="mt-1.5 flex items-center gap-2">
                    <ChannelTag id={promo.channel} />
                    <span className="text-[11.5px] text-ink-3">{promo.type}</span>
                    <PromoStatus status={promo.status} />
                  </div>
                </div>
                <div className="rounded-lg bg-accent-soft px-2.5 py-1 text-[13px] font-semibold text-accent">{promo.discount}</div>
              </div>
              <div className="mt-3 text-[12px] text-ink-3">
                {promo.startDate} ~ {promo.endDate}
              </div>
              <div className="mt-3">
                <div className="mb-1.5 flex items-center justify-between text-[12px]">
                  <span className="text-ink-3">销售进度</span>
                  <span className="text-ink-2">
                    ¥{promo.sales.toLocaleString()} / ¥{promo.target.toLocaleString()}
                  </span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-inset">
                  <div className="h-full rounded-full transition-all" style={{ width: `${progress}%`, backgroundColor: barColor }} />
                </div>
                <div className="mt-1 text-[11px] text-ink-3">{progress.toFixed(0)}% 完成</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PromoStatus({ status }: { status: 'active' | 'upcoming' | 'expired' }) {
  if (status === 'active') return <span className="text-[11.5px] font-medium text-ok">进行中</span>;
  if (status === 'upcoming') return <span className="text-[11.5px] font-medium text-accent">未开始</span>;
  return <span className="text-[11.5px] text-ink-3">已结束</span>;
}

/* ============================== 任务看板 ============================== */

const TASK_COLUMNS: { id: Task['status']; title: string; dot: string }[] = [
  { id: 'todo', title: '待处理', dot: 'bg-ink-3' },
  { id: 'doing', title: '进行中', dot: 'bg-warn' },
  { id: 'done', title: '已完成', dot: 'bg-ok' },
];

const PRIORITY_META = {
  high: { label: '高', color: '#DC2626' },
  medium: { label: '中', color: '#D97706' },
  low: { label: '低', color: '#16803C' },
} as const;

function TasksTab() {
  const [tasks, setTasks] = useState<Task[]>(INITIAL_TASKS);
  const [dragId, setDragId] = useState<string | null>(null);

  const drop = useCallback(
    (target: Task['status']) => {
      if (!dragId) return;
      setTasks((prev) =>
        prev.map((t) =>
          t.id === dragId
            ? { ...t, status: target, progress: target === 'done' ? 100 : target === 'todo' ? 0 : t.progress }
            : t,
        ),
      );
      setDragId(null);
    },
    [dragId],
  );

  return (
    <div className="flex animate-fade-up gap-4 overflow-x-auto pb-2">
      {TASK_COLUMNS.map((col) => {
        const list = tasks.filter((t) => t.status === col.id);
        return (
          <div
            key={col.id}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => drop(col.id)}
            className="w-full min-w-[260px] rounded-xl border border-line bg-inset/60 p-3"
          >
            <div className="mb-3 flex items-center gap-2 px-1">
              <span className={`h-2 w-2 rounded-full ${col.dot}`} />
              <span className="text-[12.5px] font-medium text-ink">{col.title}</span>
              <span className="text-[11px] text-ink-3">{list.length}</span>
            </div>
            <div className="space-y-2.5">
              {list.map((task) => {
                const meta = CHANNEL_META[task.channel];
                const pr = PRIORITY_META[task.priority];
                return (
                  <div
                    key={task.id}
                    draggable
                    onDragStart={() => setDragId(task.id)}
                    onDragEnd={() => setDragId(null)}
                    className={`cursor-grab rounded-lg border border-line bg-elevated p-3 shadow-card transition-all hover:border-line-strong active:cursor-grabbing ${
                      dragId === task.id ? 'opacity-50' : ''
                    }`}
                  >
                    <div className="text-[13px] font-medium text-ink">{task.title}</div>
                    <div className="mt-2 flex items-center gap-1.5">
                      <span
                        className="rounded px-1.5 py-px text-[10.5px] font-medium"
                        style={{ backgroundColor: `${meta.color}14`, color: meta.color }}
                      >
                        {meta.label}
                      </span>
                      <span
                        className="rounded px-1.5 py-px text-[10.5px] font-medium"
                        style={{ backgroundColor: `${pr.color}14`, color: pr.color }}
                      >
                        {pr.label}优先
                      </span>
                    </div>
                    {task.progress > 0 && task.progress < 100 && (
                      <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-inset">
                        <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${task.progress}%` }} />
                      </div>
                    )}
                  </div>
                );
              })}
              {list.length === 0 && <div className="py-6 text-center text-[12px] text-ink-3">拖拽任务到这里</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
