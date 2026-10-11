"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import AskPanel from "@/components/AskPanel";
import GraphExplorer from "@/components/GraphExplorer";
import FileBrowser from "@/components/FileBrowser";
import ImpactPanel from "@/components/ImpactPanel";
import { EmbeddingNote } from "@/components/SetupStatus";
import StatusBadge from "@/components/StatusBadge";
import { api, pct, post } from "@/lib/api";
import { type Build, type Dataset, FAMILY_LABEL, FAMILIES, type SymbolAudit } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

interface Quality {
  unresolved_call_rate: number | null;
  name_only_rate: number | null;
  call_sites: number | null;
  calls_by_resolver: Record<string, number> | null;
  external_calls: number | null;
  counts: { nodes: Record<string, number>; edges: Record<string, number> } | null;
  history: Record<string, number> | null;
  github: Record<string, unknown> | null;
  embeddings: Record<string, unknown> | null;
  edge_audit: Record<string, { checked: number; correct: number; precision: number | null }>;
  symbol_audit: SymbolAudit | null;
  semantic_error?: string | null;
}

function Tile({ label, value, note }: { label: string; value: React.ReactNode; note?: string }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

/** Where call sites went: precise resolvers, bare-name guesses, unresolved, external (part-to-whole). */
function ResolutionBar({ q }: { q: Quality }) {
  const by = q.calls_by_resolver ?? {};
  const precise = Object.entries(by).filter(([resolver]) => resolver !== "name").reduce((sum, [, count]) => sum + count, 0);
  const name = by.name ?? 0;
  const external = q.external_calls ?? 0;
  const unresolved = Math.max(0, (q.call_sites ?? 0) - precise - name - external);
  const parts = [
    { label: "resolved from code", n: precise, color: "var(--series-1)" },
    { label: "name guess", n: name, color: "var(--series-2)" },
    { label: "unresolved / dynamic", n: unresolved, color: "var(--series-3)" },
    { label: "external (stdlib, deps)", n: external, color: "var(--baseline)" },
  ];
  const total = parts.reduce((s, p) => s + p.n, 0) || 1;
  return (
    <div>
      <div className="stackbar" role="img" aria-label="Call sites by resolution">
        {parts.filter((p) => p.n > 0).map((p) => (
          <span key={p.label} style={{ width: `${(100 * p.n) / total}%`, background: p.color }} title={`${p.label}: ${p.n}`} />
        ))}
      </div>
      <div className="legend" style={{ marginTop: 8 }}>
        {parts.map((p) => (
          <span key={p.label} className="key">
            <span className="swatch-rect" style={{ background: p.color }} />{p.label}: <strong>{p.n}</strong>
            <span className="muted">({((100 * p.n) / total).toFixed(1)}%)</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function DatasetForm({ build, onCreated }: { build: Build; onCreated: () => void }) {
  const buildId = build.id;
  const capabilityFamilies = build.stats?.capabilities?.question_families;
  const languages = Object.keys(build.stats?.language_counts ?? { python: 1 });
  const supported = Array.isArray(capabilityFamilies) ? FAMILIES.filter((family) => capabilityFamilies.includes(family))
    : FAMILIES.filter((family) => (family !== "codegen" || languages.some((language) => language.toLowerCase() === "python"))
      && (family !== "history" || Boolean(build.options.history)) && (family !== "localization" || Boolean(build.options.github)));
  const [families, setFamilies] = useState<string[]>(supported.filter((family) => !["codegen", "history", "localization"].includes(family)));
  const [perFamily, setPerFamily] = useState(6);
  const [seed, setSeed] = useState(0);
  const [rephrase, setRephrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toggle = (f: string) => setFamilies((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await post<Dataset>("/datasets", { build_id: buildId, families, per_family: perFamily, seed,
                                         rephrase_model: rephrase.trim() || null });
      onCreated();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="card" id="evaluate" onSubmit={submit}>
      <h2>3. Optional: evaluate how well an AI understands this code</h2>
      <p className="secondary">Generate questions with reference answers from this build&apos;s static graph. Then compare model answers on the same questions. This evaluates AI answers; it does not score the repository&apos;s quality.</p>
      <div className="checks">
        {supported.map((f) => (
          <label key={f}><input type="checkbox" checked={families.includes(f)} onChange={() => toggle(f)} /> {FAMILY_LABEL[f] ?? f}</label>
        ))}
      </div>
      <div className="row" style={{ margin: "12px 0" }}>
        <label className="field">Questions per family
          <input type="number" min={1} max={200} value={perFamily} onChange={(e) => setPerFamily(Number(e.target.value))} />
        </label>
        <label className="field">Random seed (repeatable sampling)<input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label>
        <label className="field">Rephrase with LLM (optional)
          <input type="text" value={rephrase} onChange={(e) => setRephrase(e.target.value)} placeholder="Leave blank to use question templates" />
        </label>
      </div>
      <p className="secondary">A small first dataset is faster to try. Some question types need calls, tests, classes or history present in the graph; a requested count is a maximum, not a guarantee.</p>
      <button disabled={busy || !families.length}>{busy ? "Starting…" : "Generate evaluation questions"}</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

function SymbolFromQuery({ buildId }: { buildId: number }) {
  const params = useSearchParams();
  return <GraphExplorer buildId={buildId} initialSymbol={params.get("symbol")} />;
}

function DeleteBuild({ buildId }: { buildId: number }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  async function remove() {
    if (!window.confirm(`Delete build #${buildId}: its graph, checkout, datasets, experiments and labels?`)) return;
    try {
      await api(`/builds/${buildId}`, { method: "DELETE" });
      router.push("/");
    } catch (err) {
      setError(String(err));
    }
  }
  return <>{error && <span className="error">{error}</span>}<button className="danger" onClick={remove}>Delete build</button></>;
}

export default function BuildPage() {
  const { id } = useParams<{ id: string }>();
  const buildId = Number(id);
  const build = usePoll<Build>(`/builds/${buildId}`, 3000, (b) => isActive(b.status));
  const quality = usePoll<Quality>(build.data?.status === "done" ? `/builds/${buildId}/quality` : null, 0, () => false);
  const datasets = usePoll<Dataset[]>("/datasets", 4000);
  const b = build.data;
  const q = quality.data;
  const mine = (datasets.data ?? []).filter((d) => d.build_id === buildId);

  return (
    <>
      <div className="spread">
        <div>
          <h1>Build #{buildId}{b ? ` · ${b.repo}` : ""}</h1>
          <p className="subtitle">{b?.commit_sha ? <>Code snapshot: <span className="mono">{b.commit_sha.slice(0, 12)}</span>. Compare against this commit when checking results.</> : b?.status === "done" ? "Analyzed a local snapshot without a Git commit identifier." : "Building a map of this repository’s code."}</p>
        </div>
        <div className="row">
          {b && <StatusBadge status={b.status} progress={b.progress} />}
          {b && !isActive(b.status) && <DeleteBuild buildId={buildId} />}
        </div>
      </div>
      {build.error && <p className="error">{build.error}</p>}
      {b && isActive(b.status) && <div className="notice"><p>Analysis is running in the background. This page updates automatically. Large repositories take longer to resolve calls.</p><p>If progress stops, inspect <code>docker compose logs --tail=100 worker</code>. A build interrupted by a restart may need to be submitted again.</p></div>}
      {b?.status === "failed" && <section className="card"><h2>Build could not finish</h2><pre className="error">{b.error}</pre><p>Check the URL and commit, confirm the worker can access the repository, and inspect <code>docker compose logs --tail=100 worker</code>. <Link href="/">Return to repositories to try again.</Link></p></section>}

      {b?.status === "done" && <>
        <nav className="section-links" aria-label="Build actions"><a href="#files">Browse files</a><a href="#explore">Explore symbols</a><a href="#impact">Check a change</a><a href="#ask">Ask with an AI model</a><a href="#evaluate">Evaluate model answers</a></nav>
        <section className="card"><h2>Your repository map is ready</h2>
          <p>Start by finding a function or class below. Follow its callers and dependencies, then check which tests may be affected by your change.</p>
          {b.stats?.language_counts && <div className="chips">{Object.entries(b.stats.language_counts as Record<string, number>).map(([language, count]) => <span className="chip" key={language}>{language}: {count} source {count === 1 ? "file" : "files"}</span>)}</div>}
          {typeof b.stats?.structure_seconds === "number" && <p className="secondary">{b.stats.analysis_mode === "thorough" ? "Thorough" : "Fast"} analysis · Parsing and call resolution: {b.stats.structure_seconds.toFixed(1)} seconds. Checkout, graph storage and optional enrichment add time.</p>}
          <p className="secondary">The graph describes the code that static analysis could link. Missing calls, dynamic behavior and connections between languages can leave gaps. Test suggestions are a starting point; run the project&apos;s tests to verify a change.</p>
          {Array.isArray(b.stats?.warnings) && b.stats.warnings.map((warning: string) => <p className="secondary" key={warning}>{warning}</p>)}
          {b.stats?.unsupported_files?.length > 0 && <details><summary>Files without structural analysis ({b.stats?.unsupported_files.length})</summary><pre>{b.stats?.unsupported_files.slice(0, 50).map((file: unknown) => typeof file === "string" ? file : JSON.stringify(file)).join("\n")}</pre></details>}
          {b.stats?.skipped_files && <details><summary>Skipped file details</summary><pre>{JSON.stringify(b.stats.skipped_files, null, 2)}</pre></details>}
          <EmbeddingNote model={b.stats?.embeddings?.embedding_model} />
          {(b.stats?.functions ?? 0) === 0 && <div className="notice"><p>No functions were found. This may be a repository of documentation, data, source files with inventory-only support or generated code. File questions can still evaluate the indexed inventory. Check language coverage before expecting call relationships or change-impact results.</p></div>}
        </section>
      </>}

      {q && (
        <>
          <div className="tiles">
            <Tile label="Functions" value={b?.stats?.functions ?? "–"} note={`${b?.stats?.test_functions ?? 0} detected tests`} />
            <Tile label="Classes" value={b?.stats?.classes ?? "–"} note={`${b?.stats?.files ?? 0} files`} />
            <Tile label="Call sites" value={q.call_sites ?? "–"} note={`${q.counts?.edges?.CALLS ?? 0} CALLS edges`} />
            <Tile label="Unresolved call rate" value={pct(q.unresolved_call_rate)} note="of calls into repo code" />
            <Tile label="Name-only rate" value={pct(q.name_only_rate)} note="guesses excluded from reference answers" />
            <Tile label="Commits mined" value={q.history?.commits ?? "–"}
                  note={`${q.counts?.nodes?.PullRequest ?? 0} PRs · ${q.counts?.nodes?.Issue ?? 0} issues`} />
            {q.symbol_audit && (
              <Tile label="Symbol-checker false positives" value={pct(q.symbol_audit.false_positive_rate)}
                    note={`on the repo's own code: ${q.symbol_audit.functions_flagged} of ${q.symbol_audit.functions_checked} flagged`} />
            )}
          </div>
          {q.semantic_error && (
            <section className="card">
              <h2>LLM summaries and feature tags failed</h2>
              <p className="muted">
                The structure graph, history and embeddings are complete, and questions never use this layer.
                Check the model name and its provider key, then rebuild to add summaries.
              </p>
              <pre className="error">{q.semantic_error}</pre>
            </section>
          )}
          <details className="card">
            <summary>Understand call resolution and graph coverage</summary>
            <h3>How call sites were resolved</h3>
            <ResolutionBar q={q} />
            <p className="secondary">Resolved relationships come from code analysis. Name guesses are weaker and excluded from call-based reference answers. Unresolved calls are gaps; external calls point to libraries outside this repository. Percentages describe coverage, not code quality.</p>
            {Object.keys(q.edge_audit ?? {}).length > 0 && (
              <>
                <h3>Hand audit of sampled CALLS edges</h3>
                <table>
                  <thead><tr><th>Resolver</th><th className="num">Checked</th><th className="num">Precision</th></tr></thead>
                  <tbody>
                    {Object.entries(q.edge_audit).map(([r, a]) => (
                      <tr key={r}><td>{r}</td><td className="num">{a.checked}</td><td className="num">{pct(a.precision)}</td></tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </details>
        </>
      )}

      {b?.status === "done" && (
        <>
          <FileBrowser buildId={buildId} />
          <Suspense fallback={null}><SymbolFromQuery buildId={buildId} /></Suspense>
          <ImpactPanel buildId={buildId} />
          <AskPanel buildId={buildId} hasEmbeddings={Boolean(b.stats?.embeddings?.embedding_model)} />
          {(b.stats?.files ?? 0) > 0 && <DatasetForm build={b} onCreated={datasets.refresh} />}
          {mine.length > 0 && (
            <section className="card">
              <h2>Datasets from this build</h2>
              <table>
                <thead><tr><th>Dataset</th><th>Status</th><th className="num">Questions</th><th /></tr></thead>
                <tbody>
                  {mine.map((d) => (
                    <tr key={d.id}>
                      <td><Link href={`/datasets/${d.id}`}>#{d.id} {d.version}</Link></td>
                      <td><StatusBadge status={d.status} progress={d.progress} /></td>
                      <td className="num">{d.n_questions}</td>
                      <td><Link href={`/datasets/${d.id}/leaderboard`}>Leaderboard →</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}
    </>
  );
}
