import React, { useCallback, useEffect, useState } from 'react';
import { apiPost, apiSession } from './api.js';
import Login from './components/Login.jsx';
import ErrorBanner from './components/ErrorBanner.jsx';
import StressMeter from './components/StressMeter.jsx';
import PollMetrics from './components/PollMetrics.jsx';
import AssignmentList from './components/AssignmentList.jsx';
import DailySchedule from './components/DailySchedule.jsx';
import ProgressTracker from './components/ProgressTracker.jsx';

export default function App() {
  const [auth, setAuth] = useState('checking');   // 'checking' | 'locked' | 'open'

  useEffect(() => {
    apiSession().then((r) => setAuth(r.ok ? 'open' : 'locked')).catch(() => setAuth('locked'));
    const lock = () => setAuth('locked');
    window.addEventListener('mimir-unauthorized', lock);
    return () => window.removeEventListener('mimir-unauthorized', lock);
  }, []);

  const logout = useCallback(async (e) => {
    e.preventDefault();
    try { await apiPost('/logout'); } catch (err) { /* the cookie is only cleared on success; the password page still shows */ }
    setAuth('locked');
  }, []);

  if (auth === 'checking') return null;
  if (auth === 'locked') return <Login onUnlock={() => setAuth('open')} />;
  return (
    <>
      <ErrorBanner />
      <div className="topbar"><a href="#logout" onClick={logout}>Log out</a></div>
      <main className="layout">
        <div className="col">
          <StressMeter />
          <PollMetrics />
        </div>
        <div className="col">
          <AssignmentList />
          <DailySchedule />
        </div>
        <div className="full">
          <ProgressTracker />
        </div>
      </main>
    </>
  );
}
