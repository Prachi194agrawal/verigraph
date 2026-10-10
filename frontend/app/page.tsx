"use client";

import Link from "next/link";
import { useState } from "react";
import StatusBadge from "@/components/StatusBadge";
import { pct, post } from "@/lib/api";
import type { Build, Dataset, Repo } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

export default function Home() {
  const builds = usePoll<Build[]>("/builds", 3000);
  const datasets = usePoll<Dataset[]>("/datasets", 3000);
  const [url, setUrl] = useState("");
  const [commit, setCommit] = useState("");
  const [history, setHistory] = useState(true);
  const [github, setGithub] = useState(true);
  const [embeddings, setEmbeddings] = useState(true);
  const [semanticModel, setSemanticModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const repo = await post<Repo>("/repos", { url: url.trim(), commit_sha: commit.trim() || null });
      await post<Build>(`/repos/${repo.id}/builds`, {
        history, github, embeddings, semantic_model: semanticModel.trim() || null,
      });
      setUrl("");
      setCommit("");
      builds.refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Repositories &amp; graph builds</h1>
      <p className="subtitle">
        Register a repository at a pinned commit and build its code graph (structure, history, meaning). Then
        generate a dataset of exact-answer questions and run models × pipelines against it.
      </p>

      <form className="card" onSubmit={submit}>
        <h2>Register a repository and build its graph</h2>
        <div className="row">
          <label className="field">Git URL, GitHub URL or local path
            <input type="text" required value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://github.com/psf/requests" />
          </label>
          <label className="field">Commit, tag or branch (optional)
            <input type="text" value={commit} onChange={(e) => setCommit(e.target.value)} placeholder="v2.32.3" />
          </label>
          <label className="field">LLM for summaries + feature tags (optional)
            <input type="text" value={semanticModel} onChange={(e) => setSemanticModel(e.target.value)}
                   placeholder="groq/llama-3.1-8b-instant" />
          </label>
        </div>
        <div className="checks" style={{ margin: "12px 0" }}>
          <label><input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} /> git history</label>
          <label><input type="checkbox" checked={github} onChange={(e) => setGithub(e.target.checked)} /> GitHub issues &amp; PRs</label>
          <label><input type="checkbox" checked={embeddings} onChange={(e) => setEmbeddings(e.target.checked)} /> code embeddings</label>
        </div>
        <button disabled={busy || !url.trim()}>{busy ? "Starting…" : "Build graph"}</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="card">
        <h2>Graph builds</h2>
        {builds.error && <p className="error">{builds.error}</p>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Build</th><th>Repository</th><th>Commit</th><th>Status</th><th className="num">Functions</th>
                <th className="num">Call sites</th><th className="num">Unresolved</th><th className="num">Name-only</th></tr>
            </thead>
            <tbody>
              {(builds.data ?? []).map((b) => (
                <tr key={b.id}>
                  <td><Link href={`/builds/${b.id}`}>#{b.id}</Link></td>
                  <td>{b.repo}</td>
                  <td className="mono">{b.commit_sha?.slice(0, 10) ?? "–"}</td>
                  <td><StatusBadge status={b.status} progress={b.progress} /></td>
                  <td className="num">{b.stats?.functions ?? "–"}</td>
                  <td className="num">{b.stats?.call_sites ?? "–"}</td>
                  <td className="num">{pct(b.unresolved_call_rate)}</td>
                  <td className="num">{pct(b.stats?.name_only_rate)}</td>
                </tr>
              ))}
              {builds.data?.length === 0 && <tr><td colSpan={8} className="muted">No builds yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Datasets</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Dataset</th><th>Version</th><th>Build</th><th>Status</th><th className="num">Questions</th><th /></tr></thead>
            <tbody>
              {(datasets.data ?? []).map((d) => (
                <tr key={d.id}>
                  <td><Link href={`/datasets/${d.id}`}>#{d.id}</Link></td>
                  <td className="mono">{d.version}</td>
                  <td><Link href={`/builds/${d.build_id}`}>#{d.build_id}</Link></td>
                  <td><StatusBadge status={d.status} progress={d.progress} /></td>
                  <td className="num">{d.n_questions}</td>
                  <td>{d.status === "done" && !isActive(d.status) &&
                    <Link href={`/datasets/${d.id}/leaderboard`}>Leaderboard →</Link>}</td>
                </tr>
              ))}
              {datasets.data?.length === 0 && <tr><td colSpan={6} className="muted">No datasets yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
