import React from 'react';
import { usePoll, fmtTime, Stale } from '../api.js';

const APIS = [['canvas', 'Canvas'], ['github', 'GitHub'], ['oura', 'Oura']];

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
              <div className="stat-line">last {fmtTime(m.last_poll)}</div>
              <div className="stat-line">{m.avg_response_us != null ? `${m.avg_response_us.toLocaleString()} µs avg` : 'no calls in the last hour'}</div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
