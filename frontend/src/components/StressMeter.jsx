import React from 'react';
import { usePoll, fmtTime, Stale } from '../api.js';

const MAX = 1.5;
export function bandOf(r) { return r < 0.8 ? 'green' : r < 1.0 ? 'amber' : 'red'; }

function Gauge({ ratio }) {
  const clamped = Math.max(0, Math.min(MAX, ratio));
  const a = Math.PI * (1 - clamped / MAX); // 0 -> left, MAX -> right, over a half circle
  const cx = 100, cy = 95, r = 80;
  const x = cx + r * Math.cos(a), y = cy - r * Math.sin(a);
  const arc = (from, to, cls) => {
    const a0 = Math.PI * (1 - from / MAX), a1 = Math.PI * (1 - to / MAX);
    return <path className={`arc ${cls}`} d={`M ${cx + r * Math.cos(a0)} ${cy - r * Math.sin(a0)} A ${r} ${r} 0 0 1 ${cx + r * Math.cos(a1)} ${cy - r * Math.sin(a1)}`} />;
  };
  return (
    <svg viewBox="0 0 200 110" className="gauge">
      {arc(0, 0.8, 'green')}{arc(0.8, 1.0, 'amber')}{arc(1.0, MAX, 'red')}
      <line className="needle" x1={cx} y1={cy} x2={x} y2={y} />
      <circle cx={cx} cy={cy} r="4" className="hub" />
      <text x="18" y="108" className="tick">0</text><text x="170" y="108" className="tick">{MAX}+</text>
    </svg>
  );
}

function Sparkline({ rows }) {
  if (!rows || rows.length < 2) return <p className="muted">Not enough history yet.</p>;
  const w = 240, h = 40, max = Math.max(1.5, ...rows.map((r) => r.ratio));
  const pts = rows.map((r, i) => `${(i / (rows.length - 1)) * w},${h - (r.ratio / max) * h}`).join(' ');
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="spark">
      <line x1="0" x2={w} y1={h - (1.0 / max) * h} y2={h - (1.0 / max) * h} className="ref" />
      <polyline points={pts} />
    </svg>
  );
}

export default function StressMeter() {
  const today = usePoll('/stress/today');
  const hist = usePoll('/stress/history?days=30');
  const s = today.payload;
  return (
    <section className="card">
      <h2>Stress <Stale stale={today.stale} error={today.error} /></h2>
      {s ? (
        <>
          <Gauge ratio={s.ratio} />
          <div className={`ratio band-${s.band || bandOf(s.ratio)}`}>{s.ratio.toFixed(2)} <small>{s.band || bandOf(s.ratio)}</small></div>
          <table className="kv">
            <tbody>
              <tr><th>Available</th><td>{s.t_available?.toFixed(1)} h of {s.t_awake?.toFixed(1)} awake</td></tr>
              <tr><th>Deadline term</th><td>{s.deadline_term?.toFixed(2)}</td></tr>
              <tr><th>Sleep penalty</th><td>{s.sleep_penalty?.toFixed(2)} ({s.hours_slept?.toFixed(1)} h slept)</td></tr>
              <tr><th>Calculated</th><td>{fmtTime(s.calculated_at)}</td></tr>
            </tbody>
          </table>
        </>
      ) : <p className="muted">No stress score yet.</p>}
      <h3>Last 30 days</h3>
      <Sparkline rows={hist.payload} />
    </section>
  );
}
