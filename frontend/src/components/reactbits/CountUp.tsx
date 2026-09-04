interface CountUpProps {
  /** 目标数值（仅支持整数） */
  to: number;
  /** 动画时长（秒） */
  duration?: number;
  className?: string;
}

/**
 * React Bits「Count Up」的本地 CSS 适配版：
 * 用 @property + counter 实现纯 CSS 数字滚动，
 * 时间锚定 —— 后台/节流环境下同样稳定到达最终值。
 * 视觉参考：https://reactbits.dev/text-animations/count-up
 */
const CountUp = ({ to, duration = 1.2, className = '' }: CountUpProps) => {
  return (
    <span
      className={`count-up ${className}`}
      style={{ '--cu-target': to, animationDuration: `${duration}s` } as React.CSSProperties}
    />
  );
};

export default CountUp;
