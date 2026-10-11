"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { EmbeddingNote, LanguageCoverage, useCapabilities } from "@/components/SetupStatus";
import StatusBadge from "@/components/StatusBadge";
import { pct, post } from "@/lib/api";
import type { Build, Dataset, Repo } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

export default function Home() {
  const router = useRouter();
  const capabilities = useCapabilities();
  const builds = usePoll<Build[]>("/builds", 3000);
  const datasets = usePoll<Dataset[]>("/datasets", 3000);
  const [url, setUrl] = useState("");
  const [commit, setCommit] = useState("");
  const [history, setHistory] = useState(false);
  const [github, setGithub] = useState(false);
  const [embeddings, setEmbeddings] = useState(true);
  const [semanticModel, setSemanticModel] = useState("");
  const [analysisMode, setAnalysisMode] = useState("fast");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const repo = await post<Repo>("/repos", { url: url.trim(), commit_sha: commit.trim() || null });
      const build = await post<Build>(`/repos/${repo.id}/builds`, {
        history, github, embeddings, semantic_model: semanticModel.trim() || null, analysis_mode: analysisMode,
      });
      setUrl("");
      setCommit("");
      builds.refresh();
      router.push(`/builds/${build.id}`);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Understand a repository before you change it</h1>
      <p className="subtitle">
        Map functions, classes and calls. Find where code lives, who uses it, and which tests a change may affect.
        You can explore the knowledge graph without an AI key.
      </p>

      <ol className="steps" aria-label="Getting started">
        <li><strong>1. Add your repository</strong><span>Paste a public Git URL. The server downloads and analyzes the code.</span></li>
        <li><strong>2. Understand the code</strong><span>Open the completed build to explore symbols, read source, and check change impact.</span></li>
        <li><strong>3. Evaluate AI answers</strong><span>Optionally generate reference questions and compare models using the same repository.</span></li>
      </ol>

      <form className="card" onSubmit={submit}>
        <h2>1. Build your repository&apos;s knowledge graph</h2>
        <div className="row">
          <label className="field grow">Repository URL
            <input type="text" required value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://github.com/your-name/your-repository" />
          </label>
          <label className="field">Commit, tag or branch (optional)
            <input type="text" value={commit} onChange={(e) => setCommit(e.target.value)} placeholder="Default branch if blank" />
          </label>
        </div>
        <p className="secondary">Start with the default settings for a faster first build. Pin a commit when you want to reproduce results. Private repositories need Git credentials on the worker. A local path must be accessible inside the worker.</p>
        <details>
          <summary>Optional history, GitHub data, and AI summaries</summary>
          <label className="field" style={{ marginTop: 12 }}>Analysis mode
            <select value={analysisMode} onChange={(e) => setAnalysisMode(e.target.value)}>
              <option value="fast">Fast: syntax and scoped symbol resolution</option>
              <option value="thorough">Thorough: additional Python type inference (slower)</option>
            </select>
          </label>
          <div className="checks" style={{ margin: "12px 0" }}>
            <label><input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} /> Analyze Git history</label>
            <label><input type="checkbox" checked={github} onChange={(e) => setGithub(e.target.checked)} /> Import GitHub issues &amp; pull requests</label>
            <label><input type="checkbox" checked={embeddings} onChange={(e) => setEmbeddings(e.target.checked)} /> Build code search embeddings</label>
          </div>
          <label className="field">LLM for summaries + feature tags (optional)
            <input type="text" value={semanticModel} onChange={(e) => setSemanticModel(e.target.value)}
                   placeholder="Exact model name configured on your server" />
          </label>
          <p className="secondary">History and GitHub imports add work. AI summaries require a real provider and may incur usage costs; leave the model blank to skip them.</p>
        </details>
        <EmbeddingNote model={capabilities.data?.embedding_model} />
        <LanguageCoverage capabilities={capabilities.data} />
        <button disabled={busy || !url.trim()}>{busy ? "Starting…" : "Build graph"}</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="card">
        <h2>Graph builds</h2>
        <p className="secondary">Open a completed build to start exploring. Building maps code; it does not run the repository&apos;s application or tests.</p>
        {builds.error && <div className="notice"><p className="error">Cannot reach the API: {builds.error}</p><p>From the VeriGraph folder, run <code>docker compose up -d --no-build</code>, then check <code>docker compose logs --tail=50 api worker</code>.</p></div>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Build</th><th>Repository</th><th>Commit</th><th>Status</th><th className="num">Functions</th>
                <th className="num">Call sites</th><th className="num">Unresolved</th><th className="num">Name-only</th></tr>
            </thead>
            <tbody>
              {(builds.data ?? []).map((b) => (
                <tr key={b.id}>
                  <td><Link href={`/builds/${b.id}`}>#{b.id}{b.status === "done" ? " · Explore" : " · Progress"}</Link></td>
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
        <details style={{ marginTop: 12 }}><summary>What do the graph statistics mean?</summary>
          <p><strong>Functions</strong> includes methods and detected test functions. <strong>Call sites</strong> are places that invoke functions. <strong>Unresolved</strong> calls could not be linked to a repository target. <strong>Name-only</strong> links are guesses from names; reference answers exclude those guesses. These percentages describe analysis coverage, not repository code quality.</p>
        </details>
      </section>

      <section className="card">
        <h2>AI evaluation datasets</h2>
        <p className="secondary">Questions with graph-derived reference answers. Generate a dataset from a completed build when you want to compare model answers.</p>
        {datasets.error && <p className="error">{datasets.error}</p>}
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
