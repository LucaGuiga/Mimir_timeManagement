import React from 'react';
import { usePoll, Stale } from '../api.js';

const START = 6 * 60, END = 24 * 60;
const toMin = (t) => { const [h, m] = String(t || '0:0').split(':').map(Number); return h * 60 + (m || 0); };
const group = (c) => (c === 'class' || c === 'fixed' ? 'hard' : c === 'clubs' ? 'clubs' : c === 'flexible' ? 'flex' : 'grey');

export default function DailySchedule() {
  const { payload, stale, error } = usePoll('/schedule/today');
  const blocks = payload || [];
  const hours = [];
  for (let h = 6; h <= 24; h += 2) hours.push(h);
  return (
    <section className="card">
      <h2>Today <Stale stale={stale} error={error} /></h2>
      {blocks.length === 0 && <p className="muted">No blocks today.</p>}
      <div className="timeline">
        {hours.map((h) => (
          <div key={h} className="hour" style={{ top: `${((h * 60 - START) / (END - START)) * 100}%` }}>{String(h).padStart(2, '0')}:00</div>
        ))}
        {blocks.map((b, i) => {
          const s = Math.max(START, toMin(b.start_time)), e = Math.min(END, toMin(b.end_time));
          if (e <= s) return null;
          return (
            <div key={i} className={`block ${group(b.time_category)} ${b.skipped ? 'skipped' : ''}`}
              title={b.skipped ? `skipped: ${b.skip_reason || ''}` : `${b.time_category}${b.moveable ? ', moveable' : ''}`}
              style={{ top: `${((s - START) / (END - START)) * 100}%`, height: `${((e - s) / (END - START)) * 100}%` }}>
              <span>{String(b.start_time).slice(0, 5)} {b.label}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}
