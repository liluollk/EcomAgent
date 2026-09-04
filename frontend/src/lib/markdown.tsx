import type { ReactNode } from 'react';

/**
 * 轻量 Markdown 渲染：支持代码块、标题、有序/无序列表、加粗、斜体、行内代码、链接。
 * 全部通过 React 元素输出，不使用 dangerouslySetInnerHTML。
 */

const INLINE_PATTERN =
  /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\([^)\s]+\))/g;

function renderInline(text: string, keyBase: string): ReactNode[] {
  return text
    .split(INLINE_PATTERN)
    .filter(Boolean)
    .map((part, i) => {
      const key = `${keyBase}-${i}`;
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
        return (
          <strong key={key} className="font-semibold text-ink">
            {part.slice(2, -2)}
          </strong>
        );
      }
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        return (
          <code key={key} className="mx-0.5 rounded bg-inset px-1.5 py-0.5 text-[0.86em] text-ink">
            {part.slice(1, -1)}
          </code>
        );
      }
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
      if (link) {
        return (
          <a key={key} href={link[2]} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            {link[1]}
          </a>
        );
      }
      if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
        return <em key={key}>{part.slice(1, -1)}</em>;
      }
      return <span key={key}>{part}</span>;
    });
}

const isBlockBoundary = (line: string) =>
  line.trim() === '' ||
  line.trim().startsWith('```') ||
  /^(#{1,3})\s+/.test(line) ||
  /^\s*([-*]|\d+\.)\s+/.test(line);

export function Markdown({ text }: { text: string }) {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let k = 0;

  while (i < lines.length) {
    const line = lines[i];

    /* 代码块 */
    if (line.trim().startsWith('```')) {
      const lang = line.trim().slice(3).trim();
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push(
        <pre
          key={k}
          className="my-2 overflow-x-auto rounded-lg border border-line bg-inset px-3.5 py-2.5 text-[12.5px] leading-relaxed"
        >
          {lang ? <div className="mb-1 text-[11px] text-ink-3">{lang}</div> : null}
          <code>{buf.join('\n')}</code>
        </pre>,
      );
      k += 1;
      continue;
    }

    /* 标题 */
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      const size = heading[1].length === 1 ? 'text-[17px]' : heading[1].length === 2 ? 'text-[15px]' : 'text-[14px]';
      blocks.push(
        <div key={k} className={`mb-1 mt-3 font-semibold text-ink first:mt-0 ${size}`}>
          {renderInline(heading[2], `h${k}`)}
        </div>,
      );
      k += 1;
      i += 1;
      continue;
    }

    /* 列表 */
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const items: { text: string; no: string }[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        const m = /^\s*([-*]|(\d+)\.)\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        items.push({ text: m[3], no: m[2] ?? '' });
        i += 1;
      }
      blocks.push(
        <ul key={k} className="my-1.5 space-y-1">
          {items.map((it, idx) => (
            <li key={idx} className="flex gap-2">
              {it.no ? (
                <span className="tabular-nums text-ink-2">{it.no}.</span>
              ) : (
                <span className="mt-[9px] h-1 w-1 flex-shrink-0 rounded-full bg-ink-3" />
              )}
              <span>{renderInline(it.text, `li${k}-${idx}`)}</span>
            </li>
          ))}
        </ul>,
      );
      k += 1;
      continue;
    }

    /* 空行 */
    if (line.trim() === '') {
      i += 1;
      continue;
    }

    /* 普通段落 */
    const buf: string[] = [];
    while (i < lines.length && !isBlockBoundary(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={k} className="my-1.5 whitespace-pre-wrap first:mt-0">
        {renderInline(buf.join('\n'), `p${k}`)}
      </p>,
    );
    k += 1;
  }

  return <div className="text-[14px] leading-[1.7] text-ink">{blocks}</div>;
}
