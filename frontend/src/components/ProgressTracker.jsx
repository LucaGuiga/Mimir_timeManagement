import React, { useMemo, useState } from 'react';
import { usePoll, Stale } from '../api.js';

const DAYS = 14;
const dayKey = (d) => d.toISOString().slice(0, 10);

function buildTree(rows) {
  const days = [];
  const now = new Date();
  for (let i = DAYS - 1; i >= 0; i--) { const d = new Date(now); d.setDate(now.getDate() - i); days.push(dayKey(d)); }
  const tree = {};
  for (const r of rows) {
    if (r.no_commit || !r.commit_timestamp) continue;
    const course = tree[r.course_name] || (tree[r.course_name] = {});
    const title = r.assignment_title || '(unmatched)';
    const a = course[title] || (course[title] = { perDay: Object.fromEntries(days.map((d) => [d, 0])), files: 0, total: 0 });
    const k = dayKey(new Date(r.commit_timestamp));
    if (k in a.perDay) a.perDay[k] += r.size_delta || 0;
    a.files += 1;
    a.total += r.size_delta || 0;
  }
  return { days, tree };
}

export default function ProgressTracker() {
  const { payload, stale, error } = usePoll('/commits/recent');
  const { days, tree } = useMemo(() => buildTree(payload || []), [payload]);
  const [open, setOpen] = useState({});
  const courses = Object.keys(tree).sort();
  return (
    <section className="card">
      <h2>Progress (last {DAYS} days, recent commits) <Stale stale={stale} error={error} /></h2>
      {courses.length === 0 && <p className="muted">No commits logged yet.</p>}
      {courses.map((c) => {
        const isOpen = open[c] !== false;
        return (
          <div key={c} className="accordion">
            <button className="acc-head" onClick={() => setOpen({ ...open, [c]: !isOpen })}>{isOpen ? '▾' : '▸'} {c}</button>
            {isOpen && Object.entries(tree[c]).sort().map(([title, a]) => {
              const max = Math.max(1, ...days.map((d) => Math.abs(a.perDay[d])));
              return (
                <div key={title} className="prog-row">
                  <div className="prog-title">{title}</div>
                  <div className="prog-days">
                    {days.map((d) => {
                      const v = a.perDay[d];
                      return v === 0
                        ? <span key={d} className="day hollow" title={`${d}: no commit`} />
                        : <span key={d} className={`day bar ${v < 0 ? 'neg' : ''}`} title={`${d}: ${v > 0 ? '+' : ''}${v} bytes`} style={{ height: `${Math.max(15, (Math.abs(v) / max) * 100)}%` }} />;
                    })}
                  </div>
                  <div className="prog-total">{a.files} file{a.files === 1 ? '' : 's'}, {a.total > 0 ? '+' : ''}{a.total} B</div>
                </div>
              );
            })}
          </div>
        );
      })}
    </section>
  );
}
