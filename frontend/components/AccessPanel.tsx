"use client";

import { useState } from "react";
import { api, setCredentials } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";

interface Access {
  username: string | null;
  authentication: boolean;
  demo_read_only: boolean;
  daily_write_limit: number;
}

export function AccessPanel() {
  const { data, error, refresh } = usePoll<Access>("/access", 0);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (data && !data.authentication && !data.demo_read_only) return null;

  async function login(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    setCredentials(username, password);
    try {
      const access = await api<Access>("/access");
      if (!access.username) throw new Error("Sign-in is not configured on this server.");
      setPassword("");
      window.dispatchEvent(new Event("verigraph-access"));
      refresh();
    } catch (err) {
      setCredentials(null);
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return <section className="notice" aria-label="Access">
    {data?.username ? <>
      <p>Signed in as <strong>{data.username}</strong>. Daily write limit: {data.daily_write_limit} requests; resets at midnight UTC.</p>
      <button onClick={() => {
        setCredentials(null);
        window.location.reload();
      }}>Sign out</button>
    </> : <>
      <p>{data?.demo_read_only ? "Read-only demo: explore analyzed repositories and results. Sign in to run analysis or make changes."
        : "Sign in to access repositories and run analysis."}</p>
      <form onSubmit={login} className="row">
        <label>Username <input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required /></label>
        <label>Password <input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required /></label>
        <button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
      <p className="secondary">Sign-in lasts for this tab until you reload it.</p>
    </>}
    {(message || (error && !error.startsWith("401:"))) && <p role="alert">{message || error}</p>}
  </section>;
}
