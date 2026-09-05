import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { Skill } from '../types';

/**
 * 技能管理器 — 运营知识包（SOP / 平台规则）的 CRUD。
 *
 * 技能是纯知识：SKILL.md（frontmatter + SOP 正文），不绑定工具；
 * 渐进式披露：菜单常驻，load_skill 命中才注入正文。
 * 内置技能只读；用户技能可新增/编辑/启停/删除。
 *
 * 独立组件：设置弹窗「技能管理」tab 与侧边栏「技能」页共用。
 */
export function SkillManager() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<Skill | null>(null);
  const [editKeywords, setEditKeywords] = useState('');
  const [editBody, setEditBody] = useState('');

  const [addName, setAddName] = useState('');
  const [addDesc, setAddDesc] = useState('');
  const [addKeywords, setAddKeywords] = useState('');
  const [addBody, setAddBody] = useState('');

  async function refresh() {
    try {
      const resp = await api('/skills');
      const data = await resp.json();
      setSkills(Array.isArray(data) ? data : []);
      setError(null);
    } catch { setError('加载技能列表失败'); }
    finally { setLoading(false); }
  }

  useEffect(() => { refresh(); }, []);

  useEffect(() => {
    if (editing) {
      setEditKeywords((editing.keywords ?? []).join(', '));
      setEditBody(editing.body ?? '');
    }
  }, [editing]);

  async function toggleEnabled(s: Skill) {
    const resp = await api(`/skills/${s.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !s.enabled }),
    });
    if (resp.ok) refresh();
  }

  async function saveEdit() {
    if (!editing) return;
    const body: Record<string, unknown> = {};
    if (editKeywords.trim()) body.keywords = editKeywords.split(',').map(s => s.trim()).filter(Boolean);
    if (editBody.trim()) body.body = editBody.trim();
    const resp = await api(`/skills/${editing.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) { setNotice(`已保存 ${data.name}`); setEditing(null); refresh(); }
    else { setNotice(data.error || '保存失败'); }
  }

  async function addSkill() {
    if (!addName.trim()) { setNotice('请填写技能标识（name）'); return; }
    const body: Record<string, unknown> = { name: addName.trim(), description: addDesc.trim() || addName.trim() };
    if (addKeywords.trim()) body.keywords = addKeywords.split(',').map(s => s.trim()).filter(Boolean);
    if (addBody.trim()) body.body = addBody.trim();
    const resp = await api('/skills', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      setNotice(`已新增技能 ${data.name}`);
      setAddName(''); setAddDesc(''); setAddKeywords(''); setAddBody('');
      refresh();
    } else { setNotice(data.error || '新增技能失败'); }
  }

  async function removeSkill(s: Skill) {
    const resp = await api(`/skills/${s.name}`, { method: 'DELETE' });
    const data = await resp.json();
    setNotice(data.error || `已删除技能 ${s.name}`);
    refresh();
  }

  return (
    <div className="space-y-4">
      <div className="text-[12.5px] font-medium text-ink">技能管理（运营知识包：SOP 操作手册 / 平台规则，load_skill 按需注入）</div>
      {loading ? <p className="text-[12px] text-ink-3">加载中…</p>
      : error ? <p className="text-[12px] text-danger">{error}</p>
      : <div className="space-y-2">
          {skills.map((s) => (
            <div key={s.name} className="rounded-xl border border-line px-4 py-3">
              <div className="flex items-center justify-between">
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className={`h-2 w-2 flex-shrink-0 rounded-full ${s.enabled ? 'bg-ok' : 'bg-ink-3/50'}`} />
                  <span className="text-[13.5px] text-ink">{s.name}</span>
                  <span className="truncate font-mono text-[11px] text-ink-3">{s.description}</span>
                  {s.builtin && <span className="flex-shrink-0 text-[11px] text-ink-3">内置只读</span>}
                </div>
                {!s.builtin && (
                  <div className="flex flex-shrink-0 items-center gap-1">
                    <button onClick={() => toggleEnabled(s)}
                      className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5">
                      {s.enabled ? '停用' : '启用'}
                    </button>
                    <button onClick={() => setEditing(editing?.name === s.name ? null : s)}
                      className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5">
                      {editing?.name === s.name ? '收起' : '编辑'}
                    </button>
                    <button onClick={() => removeSkill(s)}
                      className="rounded-md px-2 py-1 text-[11.5px] text-danger transition-colors hover:bg-red-50">删除</button>
                  </div>
                )}
              </div>
              {editing?.name === s.name && (
                <div className="mt-3 grid grid-cols-2 gap-2 border-t border-line pt-3">
                  <Field value={editKeywords} onChange={setEditKeywords} placeholder="关键词（逗号分隔）" wide />
                  <Field value={editBody} onChange={setEditBody} placeholder="SOP 正文（Markdown）" wide />
                  <div className="col-span-2 flex justify-end gap-2">
                    <button onClick={() => setEditing(null)}
                      className="h-8 rounded-lg px-3 text-[12.5px] text-ink-2 transition-colors hover:bg-black/5">取消</button>
                    <button onClick={saveEdit}
                      className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90">保存</button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>}
      {notice && <p className="text-[12px] text-ink-2">{notice}</p>}
      <div className="rounded-xl border border-line bg-black/[0.02] p-4">
        <div className="mb-2 text-[12.5px] font-medium text-ink">新增技能</div>
        <div className="grid grid-cols-2 gap-2">
          <Field value={addName} onChange={setAddName} placeholder="标识（name）" />
          <Field value={addDesc} onChange={setAddDesc} placeholder="描述" />
          <Field value={addKeywords} onChange={setAddKeywords} placeholder="关键词（逗号分隔）" wide />
          <Field value={addBody} onChange={setAddBody} placeholder="SOP 正文（Markdown）" wide />
          <button onClick={addSkill}
            className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90">添加</button>
        </div>
      </div>
    </div>
  );
}

function Field({
  value,
  onChange,
  placeholder,
  wide,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  wide?: boolean;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className={`${wide ? 'col-span-2' : ''} h-8 rounded-lg border border-line bg-white px-2.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-accent`}
    />
  );
}