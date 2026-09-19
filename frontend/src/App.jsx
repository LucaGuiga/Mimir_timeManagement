import React from 'react';
import ErrorBanner from './components/ErrorBanner.jsx';
import StressMeter from './components/StressMeter.jsx';
import PollMetrics from './components/PollMetrics.jsx';
import AssignmentList from './components/AssignmentList.jsx';
import DailySchedule from './components/DailySchedule.jsx';
import ProgressTracker from './components/ProgressTracker.jsx';

export default function App() {
  return (
    <>
      <ErrorBanner />
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
