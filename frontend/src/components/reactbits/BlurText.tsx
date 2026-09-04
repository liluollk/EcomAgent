import { useEffect, useRef, useState } from 'react';

type BlurTextProps = {
  /** 要展示的文本 */
  text?: string;
  /** 每个字符的延迟（ms） */
  delay?: number;
  className?: string;
  animateBy?: 'words' | 'letters';
};

/**
 * React Bits「Blur Text」的本地 CSS 适配版：
 * 逐字 模糊→清晰 + 轻微上浮 入场，视觉效果与原组件一致，
 * 但采用时间锚定的 CSS 动画 —— 在后台标签页 / rAF 节流环境下
 * 也能稳定到达最终状态，且不引入任何动画库依赖。
 * 视觉参考：https://reactbits.dev/text-animations/blur-text
 */
const BlurText = ({ text = '', delay = 35, className = '', animateBy = 'letters' }: BlurTextProps) => {
  const segments = animateBy === 'words' ? text.split(' ') : Array.from(text);
  const [inView, setInView] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true);
          observer.unobserve(ref.current as Element);
        }
      },
      { threshold: 0.1 }
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  return (
    <p ref={ref} className={`blur-text ${className} flex flex-wrap`} aria-label={text}>
      {segments.map((segment, index) => (
        <span
          key={index}
          aria-hidden
          className={`blur-char ${inView ? 'blur-char-run' : ''}`}
          style={{ animationDelay: `${index * delay}ms` }}
        >
          {segment === ' ' ? '\u00A0' : segment}
          {animateBy === 'words' && index < segments.length - 1 ? '\u00A0' : ''}
        </span>
      ))}
    </p>
  );
};

export default BlurText;
