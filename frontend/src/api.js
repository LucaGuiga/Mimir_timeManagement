import { createElement, useEffect, useState } from 'react';
import { API_BASE, API_TOKEN } from './config.js';

export async function apiGet(path) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  try {
    const r = await fetch(`${API_BASE}${path}`, { headers: { Authorization: `Bearer ${API_TOKEN}` }, signal: ctrl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

// Polls an endpoint every `interval` ms. Returns {payload, updatedAt, stale, error}.
export function usePoll(path, interval = 60000) {
  const [state, setState] = useState({ payload: null, updatedAt: null, stale: true, error: null });
  useEffect(() => {
    let alive = true;
    const tick = () =>
      apiGet(path)
        .then((d) => alive && setState({ payload: d.payload, updatedAt: d.updated_at, stale: d.stale, error: null }))
        .catch((e) => alive && setState((s) => ({ ...s, error: e.message })));
    tick();
    const id = setInterval(tick, interval);
    return () => { alive = false; clearInterval(id); };
  }, [path, interval]);
  return state;
}

export function fmtTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Plain createElement so this file stays .js (no JSX).
export function Stale({ stale, error }) {
  if (error) return createElement('span', { className: 'stale err' }, `error: ${error}`);
  return stale ? createElement('span', { className: 'stale' }, 'stale') : null;
}
