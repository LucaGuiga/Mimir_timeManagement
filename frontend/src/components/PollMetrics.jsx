import React from 'react';
import { usePoll, fmtTime, Stale } from '../api.js';

const APIS = [['ical', 'iCal'], ['github', 'GitHub'], ['oura', 'Oura']];

// Poll interval in minutes, e.g. "10 min", "1 min", "6 h".
function fmtMinutes(min) {
  if (min >= 120 && min % 60 === 0) return `${min / 60} h`;
  return `${Number.isInteger(min) ? min : min.toFixed(1)} min`;
}

// Response time from microseconds, e.g. "240 ms", "1.8 s".
function fmtResponse(us) {
  return us >= 1e6 ? `${(us / 1e6).toFixed(1)} s` : `${Math.round(us / 1000)} ms`;
}

export default function PollMetrics() {
  const { payload, stale, error } = usePoll('/poll_metrics/summary');
  return (
    <section className="card">
      <h2>Polling <Stale stale={stale} error={error} /></h2>
      <div className="stats">
        {APIS.map(([k, label]) => {
          const m = (payload && payload[k]) || {};
          const ok = m.state === 'running';
          return (
            <div key={k} className={`stat ${ok ? '' : 'warn'}`}>
              <div className="stat-title">{label}</div>
              <div className="stat-state">{m.state || 'unknown'}</div>
              <div className="stat-line">{m.interval_minutes != null ? `polls every ${fmtMinutes(m.interval_minutes)}` : 'not polling'}</div>
              <div className="stat-line">last {fmtTime(m.last_poll)}</div>
              <div className="stat-line">{m.avg_response_us != null ? `${fmtResponse(m.avg_response_us)} avg response` : 'no calls in the last hour'}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
