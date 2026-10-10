"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import AskPanel from "@/components/AskPanel";
import GraphExplorer from "@/components/GraphExplorer";
import ImpactPanel from "@/components/ImpactPanel";
import StatusBadge from "@/components/StatusBadge";
import { api, pct, post } from "@/lib/api";
import { type Build, type Dataset, FAMILIES, type SymbolAudit } from "@/lib/types";
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
  const precise = (by.scip ?? 0) + (by.jedi ?? 0) + (by.import ?? 0);
  const name = by.name ?? 0;
  const external = q.external_calls ?? 0;
  const unresolved = Math.max(0, (q.call_sites ?? 0) - precise - name - external);
  const parts = [
    { label: "precise (scip/jedi/import)", n: precise, color: "var(--series-1)" },
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

function DatasetForm({ buildId, onCreated }: { buildId: number; onCreated: () => void }) {
  const [families, setFamilies] = useState<string[]>([...FAMILIES]);
  const [perFamily, setPerFamily] = useState(20);
  const [seed, setSeed] = useState(0);
  const [rephrase, setRephrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const toggle = (f: string) => setFamilies((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await post<Dataset>("/datasets", { build_id: buildId, families, per_family: perFamily, seed,
                                         rephrase_model: rephrase.trim() || null });
      onCreated();
    } catch (err) {
      setError(String(err));
    }
  }
  return (
    <form className="card" onSubmit={submit}>
      <h2>Generate a dataset (step 4)</h2>
      <div className="checks">
        {FAMILIES.map((f) => (
          <label key={f}><input type="checkbox" checked={families.includes(f)} onChange={() => toggle(f)} /> {f}</label>
        ))}
      </div>
      <div className="row" style={{ margin: "12px 0" }}>
        <label className="field">Questions per family
          <input type="number" min={1} max={200} value={perFamily} onChange={(e) => setPerFamily(Number(e.target.value))} />
        </label>
        <label className="field">Seed<input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} /></label>
        <label className="field">Rephrase with LLM (optional)
          <input type="text" value={rephrase} onChange={(e) => setRephrase(e.target.value)} placeholder="groq/llama-3.1-8b-instant" />
        </label>
      </div>
      <button disabled={!families.length}>Generate questions</button>
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
          <p className="subtitle">
            {b?.commit_sha ? <>commit <span className="mono">{b.commit_sha.slice(0, 12)}</span> · </> : null}
            parser {b?.parser_version ?? "…"}
            {b ? <> · Neo4j key <span className="mono" title="the build_id property of this build's nodes in Neo4j">build_id = {b.graph_id}</span></> : null}
          </p>
        </div>
        <div className="row">
          {b && <StatusBadge status={b.status} progress={b.progress} />}
          {b && !isActive(b.status) && <DeleteBuild buildId={buildId} />}
        </div>
      </div>
      {build.error && <p className="error">{build.error}</p>}
      {b?.status === "failed" && <pre className="card error">{b.error}</pre>}

      {q && (
        <>
          <div className="tiles">
            <Tile label="Functions" value={b?.stats?.functions ?? "–"} note={`${b?.stats?.test_functions ?? 0} are tests`} />
            <Tile label="Classes" value={b?.stats?.classes ?? "–"} note={`${b?.stats?.files ?? 0} files`} />
            <Tile label="Call sites" value={q.call_sites ?? "–"} note={`${q.counts?.edges?.CALLS ?? 0} CALLS edges`} />
            <Tile label="Unresolved call rate" value={pct(q.unresolved_call_rate)} note="of calls into repo code" />
            <Tile label="Name-only rate" value={pct(q.name_only_rate)} note="never used as gold" />
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
          <section className="card">
            <h2>How call sites were resolved</h2>
            <ResolutionBar q={q} />
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
          </section>
        </>
      )}

      {b?.status === "done" && (
        <>
          <DatasetForm buildId={buildId} onCreated={datasets.refresh} />
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
          <Suspense fallback={null}><SymbolFromQuery buildId={buildId} /></Suspense>
          <AskPanel buildId={buildId} hasEmbeddings={Boolean(b.stats?.embeddings?.embedding_model)} />
          <ImpactPanel buildId={buildId} />
        </>
      )}
    </>
  );
}
