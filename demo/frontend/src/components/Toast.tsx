import { useEffect, useState } from 'react';
import { setToastListener, type ToastKind } from '../lib/toast';

interface ToastItem {
  id: number;
  message: string;
  kind: ToastKind;
}

let seq = 0;

const KIND_STYLE: Record<ToastKind, string> = {
  info: 'border-line bg-elevated text-ink shadow-pop',
  warn: 'border-[#F0D48A] bg-[#FFFBEB] text-[#92610A] shadow-pop',
  ok: 'border-[#BBE7CDAF] bg-[#ECFDF3] text-ok shadow-pop',
};

/** 全局 toast 宿主：挂载一次（App 根部），配合 lib/toast 使用。 */
export function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => {
    setToastListener((message, kind) => {
      const id = ++seq;
      setItems((prev) => [...prev.slice(-2), { id, message, kind }]);
      window.setTimeout(() => {
        setItems((prev) => prev.filter((t) => t.id !== id));
      }, 2800);
    });
    return () => setToastListener(null);
  }, []);

  if (items.length === 0) return null;

  return (
    <div className="pointer-events-none fixed left-1/2 top-4 z-[100] flex -translate-x-1/2 flex-col items-center gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          className={`animate-fade-up rounded-xl border px-4 py-2 text-[12.5px] font-medium ${KIND_STYLE[t.kind]}`}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}