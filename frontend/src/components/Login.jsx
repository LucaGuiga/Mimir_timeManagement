import React, { useState } from 'react';
import { apiPost } from '../api.js';

export default function Login({ onUnlock }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const r = await apiPost('/login', { password });
      if (r.ok) {
        setPassword('');
        onUnlock();
        return;
      }
      setError(r.status === 429 ? 'Too many attempts, try again later.' : r.status === 401 ? 'Wrong password.' : `Login failed (HTTP ${r.status}).`);
    } catch (err) {
      setError('Could not reach the server.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card login" onSubmit={submit}>
      <h2>Mimir</h2>
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
             placeholder="Password" autoComplete="current-password" autoFocus />
      <button type="submit" disabled={busy || !password}>Unlock</button>
      {error && <div className="login-error">{error}</div>}
    </form>
  );
}
