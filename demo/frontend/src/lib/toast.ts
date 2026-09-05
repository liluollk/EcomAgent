/** 轻量全局 toast：任意组件 import { toast } 即可弹出提示（未开通功能、操作反馈）。 */

export type ToastKind = 'info' | 'warn' | 'ok';

type Listener = (message: string, kind: ToastKind) => void;

let listener: Listener | null = null;

export function toast(message: string, kind: ToastKind = 'info') {
  listener?.(message, kind);
}

export function setToastListener(fn: Listener | null) {
  listener = fn;
}