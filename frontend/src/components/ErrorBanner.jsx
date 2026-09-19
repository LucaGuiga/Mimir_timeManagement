import React from 'react';
import { usePoll, fmtTime } from '../api.js';

export default function ErrorBanner() {
  const { payload } = usePoll('/errors/active');
  if (!payload || !payload.critical_count) return null;
  const l = payload.latest;
  return (
    <div className="error-banner">
      <strong>{payload.critical_count} unacknowledged critical error{payload.critical_count === 1 ? '' : 's'}.</strong>
      {l && <span> Latest {fmtTime(l.timestamp)} [{l.script_name}] {l.error_type}: {l.raw_message}</span>}
    </div>
  );
}
