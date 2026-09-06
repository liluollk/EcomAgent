/** 功能未开发占位页：入口保留，点击进入后诚实说明当前状态 */
export function ComingSoon({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-shrink-0 items-center border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">{title}</span>
        <span className="ml-auto rounded-full border border-[#F0D48A] bg-[#FFFBEB] px-2.5 py-1 text-[11px] font-medium text-[#92610A]">
          功能未开发
        </span>
      </div>
      <div className="flex flex-1 items-center justify-center px-6">
        <div className="flex w-full max-w-[460px] animate-fade-up flex-col items-center gap-3 rounded-xl bg-elevated px-6 py-14 shadow-card">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-inset text-[20px]">🚧</div>
          <div className="text-[14px] font-semibold text-ink">{title}界面未开发</div>
          <p className="text-center text-[12.5px] leading-relaxed text-ink-3">{desc}</p>
        </div>
      </div>
    </div>
  );
}