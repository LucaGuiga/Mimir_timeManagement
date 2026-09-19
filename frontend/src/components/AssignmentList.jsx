import React from 'react';
import { usePoll, fmtTime, Stale } from '../api.js';

export default function AssignmentList() {
  const { payload, stale, error } = usePoll('/assignments/upcoming');
  const rows = payload || [];
  return (
    <section className="card">
      <h2>Upcoming (14 days) <Stale stale={stale} error={error} /></h2>
      {rows.length === 0 && <p className="muted">Nothing due.</p>}
      {rows.map((a, i) => {
        const urgent = a.commit_count === 0 && a.days_remaining < 3;
        return (
          <div key={i} className={`assignment ${urgent ? 'urgent' : ''}`}>
            <div className="a-top">
              <span className="course">{a.course_name}</span>
              <span className={`tag tag-${a.assignment_type}`}>{a.assignment_type}</span>
              <span className="muted">{a.status}</span>
            </div>
            <div className="a-title">{a.title}</div>
            <div className="a-meta">
              due {fmtTime(a.due_at)} · {a.days_remaining} day{a.days_remaining === 1 ? '' : 's'} left ·{' '}
              {a.commit_count} commit{a.commit_count === 1 ? '' : 's'}{a.last_commit_at ? `, last ${fmtTime(a.last_commit_at)}` : ''}
            </div>
          </div>
        );
      })}
    </section>
  );
}
