/** 渠道、工具的展示元数据与通用格式化函数 */

export type ChannelId = 'taobao' | 'jd' | 'douyin';

export const CHANNEL_META: Record<ChannelId, { label: string; color: string }> = {
  taobao: { label: '淘宝', color: '#FF5000' },
  jd: { label: '京东', color: '#E1251B' },
  douyin: { label: '抖音', color: '#12B7BF' },
};

export const CHANNEL_LIST = (Object.keys(CHANNEL_META) as ChannelId[]).map((id) => ({
  id,
  ...CHANNEL_META[id],
}));

/** 工具名 → 中文标签与图标（feather 风格 path） */
export const TOOL_META: Record<string, { label: string; icon: string }> = {
  query_inventory: {
    label: '库存查询',
    icon: 'M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z M3.27 6.96 12 12.01l8.73-5.05 M12 22.08V12',
  },
  update_price: {
    label: '价格调整',
    icon: 'M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z M7 7h.01',
  },
  create_promotion: {
    label: '创建促销',
    icon: 'M3 11l18-5v12L3 13v-2z M11.6 16.8a3 3 0 1 1-5.8-1.6',
  },
  query_order_status: {
    label: '订单查询',
    icon: 'M1 3h15v13H1z M16 8h4l3 3v5h-7V8z M5.5 19a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z M18.5 19a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
  },
};

export function toolLabel(name: string): string {
  return TOOL_META[name]?.label ?? name;
}

/** 从工具入参推断所属渠道（sku / order_id 前缀，或显式 channel 字段） */
export function inferChannel(input: Record<string, unknown> | undefined): ChannelId | null {
  if (!input) return null;
  const ch = typeof input.channel === 'string' ? input.channel.toLowerCase() : '';
  if (ch === 'taobao' || ch === 'jd' || ch === 'douyin') return ch;
  const sku = typeof input.sku === 'string' ? input.sku.toUpperCase() : '';
  const order = typeof input.order_id === 'string' ? input.order_id.toUpperCase() : '';
  if (sku.startsWith('TB') || order.startsWith('TB')) return 'taobao';
  if (sku.startsWith('JD') || order.startsWith('JD')) return 'jd';
  if (sku.startsWith('DY') || order.startsWith('DY')) return 'douyin';
  return null;
}

/** 毫秒 → 紧凑耗时文本（<1s 显示毫秒，否则一位小数秒） */
export function durationText(ms?: number): string | null {
  if (!ms || ms <= 0) return null;
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 工具关键参数摘要：一行内联展示（craft 式 muted input summary），无关键参数返回 null */
export function paramSummary(toolName: string, input: Record<string, unknown> | undefined): string | null {
  if (!input) return null;
  const s = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);
  const parts: string[] = [];
  const sku = s(input.sku);
  const ch = s(input.channel);
  if (sku) parts.push(sku);
  switch (toolName) {
    case 'query_inventory':
    case 'update_price':
    case 'product_shelf':
      if (toolName === 'update_price' && input.new_price != null) parts.push(`→ ¥${input.new_price}`);
      if (toolName === 'product_shelf') parts.push(s(input.action) === 'off' ? '下架' : '上架');
      break;
    case 'create_promotion': {
      const title = s(input.title);
      if (title) parts.push(title);
      else if (input.discount != null) parts.push(`${input.discount} 折`);
      break;
    }
    case 'query_order_status': {
      const order = s(input.order_id);
      if (order) parts.unshift(order);
      break;
    }
    case 'query_sales':
    case 'query_statistics': {
      const metric = s(input.metric);
      if (metric) parts.push(metric);
      break;
    }
    default:
      if (parts.length === 0) {
        const first = Object.values(input).find((v) => typeof v === 'string' && (v as string).length <= 24);
        if (first) parts.push(first as string);
      }
  }
  if (ch) parts.push(CHANNEL_META[ch as ChannelId]?.label ?? ch);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** 尽量把结果格式化成可读 JSON */
export function prettyJSON(v: unknown): string {
  if (typeof v === 'string') {
    try {
      return JSON.stringify(JSON.parse(v), null, 2);
    } catch {
      return v;
    }
  }
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/** 时间戳归一：接受毫秒 / 秒 / ISO 字符串，统一为毫秒（后端混用 time.time() 与 ISO） */
export function normalizeTs(ts: number | string): number {
  const ms = typeof ts === 'string' ? new Date(ts).getTime() : ts;
  return ms < 1e12 ? ms * 1000 : ms;
}

/** 相对时间：刚刚 / x 分钟前 / HH:mm / MM-DD HH:mm */
export function relativeTime(ts: number | string): string {
  const ms = normalizeTs(ts);
  const diff = Date.now() - ms;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  const d = new Date(ms);
  const now = new Date();
  const hm = clockTime(ms);
  if (d.toDateString() === now.toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`;
}

export function clockTime(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export const uid = (): string => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
