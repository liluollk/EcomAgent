import { useCallback, useEffect, useState } from 'react';
import { CHANNEL_META } from '../../lib/format';
import { api } from '../../lib/api';
import { toast } from '../../lib/toast';
import CountUp from '../reactbits/CountUp';
import { INITIAL_TASKS, type Task } from './data';

type WorkspaceTab = 'overview' | 'products' | 'promotions' | 'tasks';

const TABS: { id: WorkspaceTab; label: string }[] = [
  { id: 'overview', label: '总览' },
  { id: 'products', label: '商品' },
  { id: 'promotions', label: '促销' },
  { id: 'tasks', label: '任务' },
];

/* ============================== 数据类型（后端 /workspace/overview） ============================== */

interface ChannelRow {
  name: string;
  label: string;
  platform: string;
  connected: boolean;
  error: string | null;
  orders: number | null;
  gmv: number | null;
  avg_order: number | null;
  refund_rate: number | null;
  tickets: number | null;
  promotions: { name: string; discount: number }[];
  anomalies: string[];
  product: { sku: string; name: string; stock: number } | null;
}

interface OverviewResp {
  channels: ChannelRow[];
  summary: {
    total_channels: number;
    connected_channels: number;
    total_orders: number;
    total_gmv: number;
    total_promotions: number;
    total_anomalies: number;
  };
}

/** 渠道展示元信息：内置三渠道有品牌色，动态渠道回退灰色 */
function channelMeta(name: string): { label: string; color: string } {
  const meta = (CHANNEL_META as Record<string, { label: string; color: string }>)[name];
  return meta ?? { label: name, color: '#64748B' };
}

function money(v: number | null): string {
  if (v === null || v === undefined) return '—';
  return `¥${Math.round(v).toLocaleString()}`;
}

function discountLabel(d: number): string {
  const zhe = Math.round(d * 100) / 10;
  return `${zhe % 1 === 0 ? zhe.toFixed(0) : zhe.toFixed(1)} 折`;
}

/** 工作台：总览 / 促销接聚合数据（展示版经 apiMock 1:1 模拟后端）；商品明细未开通；任务为本地演示看板 */
export function WorkspacePage({ onOpenChat }: { onOpenChat?: () => void }) {
  const [tab, setTab] = useState<WorkspaceTab>('overview');
  const [data, setData] = useState<OverviewResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const resp = await api('/workspace/overview');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setData((await resp.json()) as OverviewResp);
      setError(null);
    } catch {
      setError('无法加载工作台数据，请确认后端已启动');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const summary = data?.summary;
  const statusLabel = loading
    ? '加载中…'
    : error
      ? '数据加载失败'
      : summary && summary.connected_channels < summary.total_channels
        ? `${summary.connected_channels}/${summary.total_channels} 渠道在线 · 部分未接入`
        : summary
          ? `${summary.connected_channels}/${summary.total_channels} 渠道在线 · 实时数据`
          : '';

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
        <div className="flex items-center gap-2">
          <span
            className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${
              error || (summary && summary.connected_channels < summary.total_channels)
                ? 'border-[#F0D48A] bg-[#FFFBEB] text-[#92610A]'
                : 'border-[#BBE7CDAF] bg-[#ECFDF3] text-ok'
            }`}
          >
            {tab === 'tasks' ? '本地演示看板 · 拖拽交互可用' : statusLabel}
          </span>
          <button
            onClick={() => {
              setLoading(true);
              void refresh();
            }}
            title="刷新"
            className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-black/5 hover:text-ink"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div key={tab} className="mx-auto max-w-[980px] animate-fade-up px-6 py-5">
          {tab === 'overview' &&
            (error ? (
              <LoadError message={error} onRetry={refresh} />
            ) : (
              <OverviewTab data={data} loading={loading} />
            ))}
          {tab === 'products' && <ProductsTab onOpenChat={onOpenChat} />}
          {tab === 'promotions' &&
            (error ? (
              <LoadError message={error} onRetry={refresh} />
            ) : (
              <PromotionsTab data={data} loading={loading} />
            ))}
          {tab === 'tasks' && <TasksTab />}
        </div>
      </div>
    </div>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-line bg-elevated px-6 py-14 shadow-card">
      <svg className="h-8 w-8 text-ink-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <circle cx="12" cy="12" r="9" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <p className="text-[13px] text-ink-2">{message}</p>
      <button
        onClick={onRetry}
        className="h-8 rounded-lg bg-accent px-4 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
      >
        重试
      </button>
    </div>
  );
}

/* ============================== 总览（聚合数据） ============================== */

function OverviewTab({ data, loading }: { data: OverviewResp | null; loading: boolean }) {
  if (loading || !data) {
    return <div className="py-14 text-center text-[13px] text-ink-3">加载渠道数据中…</div>;
  }
  const s = data.summary;

  return (
    <div className="space-y-5 animate-fade-up">
      <div className="grid grid-cols-4 gap-4">
        <KpiCard label="近7天订单" value={s.total_orders} />
        <KpiCard label="近7天 GMV" value={s.total_gmv} money />
        <KpiCard label="进行中促销" value={s.total_promotions} tone="ok" />
        <KpiCard label="经营异常" value={s.total_anomalies} tone={s.total_anomalies > 0 ? 'warn' : undefined} />
      </div>

      {data.channels.length === 0 ? (
        <div className="rounded-xl border border-line bg-elevated px-6 py-12 text-center text-[13px] text-ink-3 shadow-card">
          暂无启用渠道，可在「设置 → 渠道连接」中新增
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {data.channels.map((c) => {
            const meta = channelMeta(c.name);
            return (
              <div key={c.name} className="relative overflow-hidden rounded-xl border border-line bg-elevated shadow-card">
                {/* 渠道品牌色顶条 */}
                <span className="absolute left-0 top-0 h-[3px] w-full" style={{ backgroundColor: c.connected ? meta.color : '#CBD5E1' }} />
                <div className="flex items-center gap-2.5 border-b border-line px-4 pb-3 pt-3.5">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: c.connected ? meta.color : '#CBD5E1' }} />
                  <span className="text-[13.5px] font-semibold text-ink">{c.label}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10.5px] font-medium ${
                      c.platform === 'mock' ? 'bg-black/5 text-ink-3' : 'bg-[#FFF4E5] text-[#B45309]'
                    }`}
                  >
                    {c.platform === 'mock' ? 'mock' : c.platform}
                  </span>
                  <span className="ml-auto text-[11.5px] text-ink-3">
                    {c.connected ? '在线' : c.error || '未接入'}
                  </span>
                </div>

                {c.connected ? (
                  <>
                    <div className="grid grid-cols-3 gap-2 px-4 py-3">
                      <MiniStat label="订单" value={c.orders} />
                      <MiniStat label="GMV" value={c.gmv} money tone="ok" />
                      <MiniStat label="客单价" value={c.avg_order} money />
                    </div>
                    <div className="border-t border-line px-4 py-2.5">
                      <div className="mb-1.5 text-[11px] font-medium text-ink-3">促销活动</div>
                      {c.promotions.length === 0 ? (
                        <div className="pb-1 text-[12px] text-ink-3">暂无进行中促销</div>
                      ) : (
                        c.promotions.slice(0, 2).map((p) => (
                          <div key={p.name} className="flex items-center justify-between py-1 text-[12px]">
                            <span className="truncate text-ink">{p.name}</span>
                            <span className="flex-shrink-0 font-medium text-accent">{discountLabel(p.discount)}</span>
                          </div>
                        ))
                      )}
                    </div>
                    <div className="border-t border-line px-4 py-2.5">
                      <div className="mb-1.5 text-[11px] font-medium text-ink-3">经营提示</div>
                      {c.anomalies.length === 0 ? (
                        <div className="pb-1 text-[12px] text-ok">运行正常，无异常项</div>
                      ) : (
                        c.anomalies.slice(0, 2).map((a) => (
                          <div key={a} className="flex items-start gap-1.5 py-0.5 text-[12px] text-ink-2">
                            <span className="mt-[5px] h-1.5 w-1.5 flex-shrink-0 rounded-full bg-warn" />
                            <span>{a}</span>
                          </div>
                        ))
                      )}
                    </div>
                    {c.product && (
                      <div className="flex items-center justify-between border-t border-line px-4 py-2.5 text-[12px]">
                        <div className="min-w-0">
                          <div className="truncate text-ink">{c.product.name}</div>
                          <div className="font-mono text-[10.5px] text-ink-3">{c.product.sku}</div>
                        </div>
                        <span className={`flex-shrink-0 font-medium ${c.product.stock <= 100 ? 'text-warn' : 'text-ink-2'}`}>
                          库存 {c.product.stock.toLocaleString()}
                        </span>
                      </div>
                    )}
                  </>
                ) : (
                  <div className="px-4 py-6 text-center text-[12px] text-ink-3">{c.error || '渠道数据不可用'}</div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function KpiCard({ label, value, tone, money: isMoney }: { label: string; value: number; tone?: 'ok' | 'warn'; money?: boolean }) {
  return (
    <div className="rounded-xl border border-line bg-elevated p-4 shadow-card">
      <div className="text-[12px] text-ink-3">{label}</div>
      <div
        className={`tnum mt-1 text-[24px] font-bold leading-none tracking-tight ${
          tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : 'text-ink'
        }`}
      >
        {isMoney ? (
          money(value)
        ) : (
          <CountUp to={value} duration={1.2} />
        )}
      </div>
    </div>
  );
}

function MiniStat({ label, value, tone, money: isMoney }: { label: string; value: number | null; tone?: 'ok'; money?: boolean }) {
  return (
    <div>
      <div className="text-[10.5px] text-ink-3">{label}</div>
      <div className={`tnum text-[14px] font-semibold ${tone === 'ok' ? 'text-ok' : 'text-ink'}`}>
        {isMoney ? money(value) : (value ?? '—')}
      </div>
    </div>
  );
}

/* ============================== 商品（未开通） ============================== */

function ProductsTab({ onOpenChat }: { onOpenChat?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-line bg-elevated px-6 py-14 shadow-card animate-fade-up">
      <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-inset text-[20px]">🚧</div>
      <div className="tnum text-[14px] font-semibold text-ink">商品明细管理未开通</div>
      <p className="max-w-[420px] text-center text-[12.5px] leading-relaxed text-ink-3">
        SKU 级商品库与批量管理依赖真实平台商品 API（适配层接入中）。
        当前可通过对话助手查询各渠道库存、价格与订单状态，总览页可查看渠道代表商品。
      </p>
      <div className="mt-1 flex items-center gap-2">
        <button
          onClick={() => {
            if (onOpenChat) {
              onOpenChat();
            } else {
              toast('请切换到「对话」页与助手交互');
            }
          }}
          className="h-8 rounded-lg bg-accent px-4 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
        >
          去对话查询库存
        </button>
        <button
          onClick={() => toast('商品批量管理规划中，接入真实平台商品 API 后开放', 'warn')}
          className="h-8 rounded-lg border border-line px-4 text-[12.5px] text-ink-2 transition-colors hover:bg-black/[0.04]"
        >
          了解更多
        </button>
      </div>
    </div>
  );
}

/* ============================== 促销（聚合数据） ============================== */

function PromotionsTab({ data, loading }: { data: OverviewResp | null; loading: boolean }) {
  const [channel, setChannel] = useState<'all' | string>('all');

  if (loading || !data) {
    return <div className="py-14 text-center text-[13px] text-ink-3">加载促销数据中…</div>;
  }

  const rows = data.channels
    .filter((c) => channel === 'all' || c.name === channel)
    .flatMap((c) => c.promotions.map((p) => ({ ...p, ch: c })));
  const channelOptions = data.channels;

  return (
    <div className="space-y-4 animate-fade-up">
      <div className="flex items-center gap-2">
        <select
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          className="h-9 rounded-lg border border-line bg-elevated px-3 text-[13px] text-ink focus:border-accent/50 focus:outline-none"
        >
          <option value="all">全部渠道</option>
          {channelOptions.map((c) => (
            <option key={c.name} value={c.name}>
              {c.label}
            </option>
          ))}
        </select>
        <span className="ml-auto text-[12px] text-ink-3">{rows.length} 个活动</span>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-xl border border-line bg-elevated px-6 py-12 text-center text-[13px] text-ink-3 shadow-card">
          所选渠道暂无进行中促销
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {rows.map((p, idx) => {
            const meta = channelMeta(p.ch.name);
            return (
              <div key={`${p.ch.name}-${p.name}-${idx}`} className="rounded-xl border border-line bg-elevated p-4 shadow-card">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate tnum text-[14px] font-semibold text-ink">{p.name}</div>
                    <div className="mt-1.5 flex items-center gap-2">
                      <span
                        className="rounded px-1.5 py-px text-[10.5px] font-medium"
                        style={{ backgroundColor: `${meta.color}14`, color: meta.color }}
                      >
                        {p.ch.label}
                      </span>
                      <span className="text-[11.5px] text-ink-3">进行中</span>
                    </div>
                  </div>
                  <div className="flex-shrink-0 rounded-lg bg-accent-soft px-2.5 py-1 text-[13px] font-semibold text-accent">
                    {discountLabel(p.discount)}
                  </div>
                </div>
                <div className="mt-3 flex items-center justify-between text-[11.5px] text-ink-3">
                  <span>数据来源：{p.ch.label}平台促销接口</span>
                  <span>创建促销可在对话页完成</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ============================== 任务看板（本地演示） ============================== */

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
                const meta = channelMeta(task.channel);
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
