import { useCallback, useEffect, useState } from 'react';
import CountUp from '../reactbits/CountUp';

interface TrendRow {
  date: string;
  gmv: number;
  orders: number;
}

interface ChannelRow {
  name: string;
  label: string;
  connected: boolean;
  gmv: number | null;
  orders: number | null;
  stock: number | null;
  product: string | null;
  anomalies: string[];
}

interface DashboardData {
  summary: {
    total_gmv: number;
    total_orders: number;
    total_stock: number;
    connected_channels: number;
    alert_count: number;
  };
  trend: TrendRow[];
  channels: ChannelRow[];
  alerts: { channel: string; text: string }[];
}

/** 渠道品牌色（数据语义色，与工具卡/渠道卡一致） */
const CHANNEL_COLOR: Record<string, string> = {
  taobao: 'var(--ch-taobao)',
  jd: 'var(--ch-jd)',
  douyin: 'var(--ch-douyin)',
};

/** 近 7 天 GMV 折线（轻量 SVG，不引图表库）。 */
function TrendLine({ trend }: { trend: TrendRow[] }) {
  if (trend.length < 2) return null;
  const w = 680;
  const h = 150;
  const pad = 8;
  const max = Math.max(...trend.map((t) => t.gmv)) * 1.1;
  const min = Math.min(...trend.map((t) => t.gmv)) * 0.9;
  const pts = trend.map((t, i) => {
    const x = pad + (i * (w - pad * 2)) / (trend.length - 1);
    const y = h - pad - ((t.gmv - min) / (max - min || 1)) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-[150px] w-full">
      <polyline
        points={pts.join(' ')}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {trend.map((t, i) => {
        const [x, y] = pts[i].split(',');
        return (
          <g key={t.date}>
            <circle cx={x} cy={y} r="3" fill="var(--bg-elevated)" stroke="var(--accent)" strokeWidth="1.5" />
            <text x={x} y={h - 0} textAnchor="middle" className="fill-[var(--text-3)] text-[10px]">
              {t.date}
            </text>
            <text x={x} y={Number(y) - 10} textAnchor="middle" className="fill-[var(--text-3)] text-[10px]">
              {Math.round(t.gmv / 1000)}k
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** 运营看板 — 跨渠道汇总概览、近 7 天销售趋势、库存水位与预警（时间维度，与工作台渠道明细互补）。 */
export function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch('/dashboard/summary');
      if (!r.ok) throw new Error('加载失败');
      setData(await r.json());
      setError(null);
    } catch {
      setError('看板数据加载失败，请确认后端已启动');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="rounded-xl border border-line bg-elevated px-6 py-10 text-center text-[12.5px] text-ink-3 shadow-card">
          {error}
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="flex flex-1 items-center justify-center text-[12.5px] text-ink-3">加载中…</div>
    );
  }

  const { summary, trend, channels, alerts } = data;
  const gmvMax = Math.max(...channels.map((c) => c.gmv ?? 0), 1);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-5 py-4">
      {/* 概览卡片行 */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: '近 7 天 GMV', value: Math.round(summary.total_gmv), prefix: '¥' },
          { label: '订单数', value: summary.total_orders, prefix: '' },
          { label: '库存总量', value: summary.total_stock, prefix: '' },
          { label: '经营预警', value: summary.alert_count, prefix: '' },
        ].map((card) => (
          <div key={card.label} className="rounded-xl border border-line bg-elevated px-4 py-3 shadow-card">
            <div className="text-[11.5px] text-ink-3">{card.label}</div>
            <div className="mt-1 text-[20px] font-semibold tabular-nums text-ink">
              {card.prefix}
              <CountUp to={card.value} />
            </div>
          </div>
        ))}
      </div>

      {/* 销售趋势 */}
      <div className="mt-4 rounded-xl border border-line bg-elevated px-4 py-3 shadow-card">
        <div className="text-[12.5px] font-medium text-ink">近 7 天全渠道 GMV 趋势</div>
        <TrendLine trend={trend} />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3">
        {/* 渠道 GMV 占比 */}
        <div className="rounded-xl border border-line bg-elevated px-4 py-3 shadow-card">
          <div className="text-[12.5px] font-medium text-ink">渠道 GMV 构成</div>
          <div className="mt-2.5 flex flex-col gap-2.5">
            {channels
              .filter((c) => c.connected)
              .map((c) => (
                <div key={c.name}>
                  <div className="flex items-center justify-between text-[11.5px] text-ink-2">
                    <span>{c.label}</span>
                    <span className="tabular-nums">¥{(c.gmv ?? 0).toLocaleString()}</span>
                  </div>
                  <div className="mt-1 h-1.5 w-full rounded-full bg-inset">
                    <div
                      className="h-1.5 rounded-full"
                      style={{
                        width: `${Math.round(((c.gmv ?? 0) / gmvMax) * 100)}%`,
                        background: CHANNEL_COLOR[c.name] ?? 'var(--accent)',
                      }}
                    />
                  </div>
                </div>
              ))}
          </div>
        </div>

        {/* 经营预警 */}
        <div className="rounded-xl border border-line bg-elevated px-4 py-3 shadow-card">
          <div className="text-[12.5px] font-medium text-ink">经营预警</div>
          <div className="mt-2.5 flex flex-col gap-1.5">
            {alerts.length === 0 && <div className="text-[12px] text-ink-3">暂无预警，经营平稳</div>}
            {alerts.map((a, i) => (
              <div
                key={i}
                className="flex items-start gap-2 rounded-lg bg-[#FFFBEB] px-3 py-1.5 text-[12px] leading-relaxed text-[#92610A]"
              >
                <span className="font-medium">{a.channel}</span>
                <span>{a.text}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* 库存水位 */}
      <div className="mt-4 rounded-xl border border-line bg-elevated px-4 py-3 shadow-card">
        <div className="text-[12.5px] font-medium text-ink">渠道库存水位</div>
        <div className="mt-2.5 grid grid-cols-3 gap-3">
          {channels.map((c) => (
            <div key={c.name} className="rounded-lg bg-inset px-3 py-2">
              <div className="flex items-center gap-1.5 text-[11.5px] text-ink-2">
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ background: c.connected ? CHANNEL_COLOR[c.name] ?? 'var(--text-3)' : 'var(--text-3)' }}
                />
                {c.label}
                {!c.connected && <span className="text-ink-3">（未连通）</span>}
              </div>
              <div className="mt-0.5 text-[13px] font-medium tabular-nums text-ink">
                {c.stock != null ? `${c.stock.toLocaleString()} 件` : '—'}
              </div>
              <div className="truncate text-[11px] text-ink-3">{c.product ?? '—'}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
