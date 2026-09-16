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

/** 工具名 → 中文标签与图标（feather 风格 path）
 *
 * 只列默认调价闭环里模型可见的工具：查快照 / 提交改价 + 技能两个元工具。
 * 其余扩展工具（工单、知识库、经营分析等）不在默认注册表里，也不会出现在主流程卡片中；
 * 遇到未列出的工具名时 `toolLabel` 回退为原名，不会渲染成空白。
 */
export const TOOL_META: Record<string, { label: string; icon: string }> = {
  query_product_snapshot: {
    label: '商品快照',
    icon: 'M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z M3.27 6.96 12 12.01l8.73-5.05 M12 22.08V12',
  },
  update_price: {
    label: '提交调价',
    icon: 'M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z M7 7h.01',
  },
  load_skill: {
    label: '加载技能',
    icon: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20 M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
  },
  save_skill: {
    label: '沉淀技能',
    icon: 'M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z M17 21v-8H7v8 M7 3v5h8',
  },
};

export function toolLabel(name: string): string {
  return TOOL_META[name]?.label ?? name;
}

/** 从工具入参推断所属渠道（新契约用 platform 字段；兼容旧 channel 字段与 SKU / 订单前缀） */
export function inferChannel(input: Record<string, unknown> | undefined): ChannelId | null {
  if (!input) return null;
  const raw =
    typeof input.platform === 'string'
      ? input.platform
      : typeof input.channel === 'string'
        ? input.channel
        : '';
  const ch = raw.toLowerCase();
  if (ch === 'taobao' || ch === 'jd' || ch === 'douyin') return ch;
  const skuVal = typeof input.sku_id === 'string' ? input.sku_id : input.sku;
  const sku = typeof skuVal === 'string' ? skuVal.toUpperCase() : '';
  const order = typeof input.order_id === 'string' ? input.order_id.toUpperCase() : '';
  if (sku.startsWith('TB') || order.startsWith('TB')) return 'taobao';
  if (sku.startsWith('JD') || order.startsWith('JD')) return 'jd';
  if (sku.startsWith('DY') || order.startsWith('DY')) return 'douyin';
  return null;
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

/** 相对时间：刚刚 / x 分钟前 / HH:mm / MM-DD HH:mm */
export function relativeTime(ts: number): string {
  const diff = Date.now() - ts;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  const d = new Date(ts);
  const now = new Date();
  const hm = clockTime(ts);
  if (d.toDateString() === now.toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`;
}

export function clockTime(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export const uid = (): string => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
