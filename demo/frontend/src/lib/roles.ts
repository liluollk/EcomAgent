/** 多角色身份常量 — 与后端 permission/rbac.py 保持一致 */

export const ROLE_OPTIONS = [
  { value: 'manager', label: '店长' },
  { value: 'operator', label: '运营专员' },
  { value: 'customer_service', label: '客服' },
  { value: 'finance', label: '财务' },
] as const;

export const ROLE_LABELS: Record<string, string> = {
  manager: '店长',
  operator: '运营专员',
  customer_service: '客服',
  finance: '财务',
};