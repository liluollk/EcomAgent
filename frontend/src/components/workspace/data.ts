/** 任务看板演示数据（本地交互）。业务数据由后端 /workspace/overview 提供 */

export interface Task {
  id: string;
  title: string;
  channel: string;
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
