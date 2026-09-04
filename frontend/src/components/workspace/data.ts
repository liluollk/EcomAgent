import { CHANNEL_META, TOOL_META, type ChannelId } from '../../lib/format';

/** 以下为演示数据，接入真实渠道后由后端数据替换 */

export interface Product {
  id: string;
  name: string;
  channel: ChannelId;
  price: number;
  stock: number;
  sales: number;
  status: 'normal' | 'low_stock' | 'out_of_stock';
  image: string;
}

export const PRODUCTS: Product[] = [
  { id: 'TB-001', name: '防晒霜 SPF50+ PA++++', channel: 'taobao', price: 89, stock: 230, sales: 156, status: 'normal', image: '☀️' },
  { id: 'TB-002', name: '保湿面膜 10 片装', channel: 'taobao', price: 59, stock: 45, sales: 89, status: 'normal', image: '💧' },
  { id: 'JD-001', name: '智能手表 运动版', channel: 'jd', price: 299, stock: 120, sales: 67, status: 'normal', image: '⌚' },
  { id: 'JD-002', name: '无线蓝牙耳机', channel: 'jd', price: 149, stock: 0, sales: 234, status: 'out_of_stock', image: '🎧' },
  { id: 'DY-001', name: '网红同款连衣裙', channel: 'douyin', price: 129, stock: 32, sales: 312, status: 'low_stock', image: '👗' },
  { id: 'DY-002', name: '男士休闲裤', channel: 'douyin', price: 99, stock: 88, sales: 145, status: 'normal', image: '👖' },
];

export interface Promotion {
  id: string;
  name: string;
  channel: ChannelId;
  type: string;
  discount: string;
  startDate: string;
  endDate: string;
  status: 'active' | 'upcoming' | 'expired';
  sales: number;
  target: number;
}

export const PROMOTIONS: Promotion[] = [
  { id: 'PROMO-001', name: '夏季清仓大促', channel: 'taobao', type: '满减', discount: '满 200 减 50', startDate: '2025-07-01', endDate: '2025-07-31', status: 'active', sales: 12500, target: 20000 },
  { id: 'PROMO-002', name: '京东 818 购物节', channel: 'jd', type: '折扣', discount: '全场 8 折', startDate: '2025-08-15', endDate: '2025-08-20', status: 'upcoming', sales: 0, target: 50000 },
  { id: 'PROMO-003', name: '抖音直播带货', channel: 'douyin', type: '秒杀', discount: '限时 5 折', startDate: '2025-07-15', endDate: '2025-07-16', status: 'expired', sales: 8900, target: 10000 },
  { id: 'PROMO-004', name: '新品首发优惠', channel: 'taobao', type: '赠品', discount: '买一送一', startDate: '2025-07-20', endDate: '2025-08-10', status: 'active', sales: 5600, target: 15000 },
];

export interface ChannelSummary {
  id: ChannelId;
  products: number;
  alerts: number;
  todaySales: number;
  top: { id: string; name: string; stock: number; price: number; sales: number }[];
}

export const CHANNEL_SUMMARIES: ChannelSummary[] = [
  {
    id: 'taobao',
    products: 128,
    alerts: 5,
    todaySales: 342,
    top: [
      { id: 'TB-001', name: '防晒霜 SPF50+', stock: 230, price: 89, sales: 156 },
      { id: 'TB-002', name: '保湿面膜 10 片装', stock: 12, price: 49, sales: 98 },
      { id: 'TB-003', name: '美白精华液 30ml', stock: 45, price: 129, sales: 88 },
    ],
  },
  {
    id: 'jd',
    products: 96,
    alerts: 2,
    todaySales: 218,
    top: [
      { id: 'JD-001', name: '智能手表 Pro', stock: 88, price: 1299, sales: 67 },
      { id: 'JD-002', name: '无线蓝牙耳机', stock: 8, price: 299, sales: 89 },
      { id: 'JD-003', name: '充电宝 20000mAh', stock: 156, price: 79, sales: 62 },
    ],
  },
  {
    id: 'douyin',
    products: 64,
    alerts: 8,
    todaySales: 567,
    top: [
      { id: 'DY-001', name: '网红零食大礼包', stock: 5, price: 39, sales: 234 },
      { id: 'DY-002', name: '便携榨汁杯', stock: 42, price: 69, sales: 189 },
      { id: 'DY-003', name: 'ins 风手机壳', stock: 3, price: 19, sales: 144 },
    ],
  },
];

export interface Task {
  id: string;
  title: string;
  channel: ChannelId;
  priority: 'high' | 'medium' | 'low';
  progress: number;
  status: 'todo' | 'doing' | 'done';
}

export const INITIAL_TASKS: Task[] = [
  { id: 'T1', title: '查询淘宝防晒霜库存', channel: 'taobao', priority: 'high', progress: 0, status: 'todo' },
  { id: 'T2', title: '调整京东耳机售价至 299 元', channel: 'jd', priority: 'medium', progress: 0, status: 'todo' },
  { id: 'T3', title: '抖音零食促销活动策划', channel: 'douyin', priority: 'high', progress: 60, status: 'doing' },
  { id: 'T4', title: '淘宝面膜补货申请', channel: 'taobao', priority: 'low', progress: 30, status: 'doing' },
  { id: 'T5', title: '京东充电宝价格优化', channel: 'jd', priority: 'medium', progress: 100, status: 'done' },
  { id: 'T6', title: '抖音手机壳上新', channel: 'douyin', priority: 'low', progress: 100, status: 'done' },
];

export const TOOL_PROMPTS = Object.keys(TOOL_META);
export { CHANNEL_META };
