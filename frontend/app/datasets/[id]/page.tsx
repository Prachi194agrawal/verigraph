"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { EmbeddingNote, ModelStatus, useCapabilities } from "@/components/SetupStatus";
import StatusBadge from "@/components/StatusBadge";
import { fmt, post } from "@/lib/api";
import { type Build, type Dataset, type Experiment, FAMILY_LABEL, FAMILIES, PIPELINE_HELP, PIPELINE_LABEL, PIPELINES, type Question } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

function ExperimentForm({ datasetId, families, build, onCreated }: { datasetId: number; families: string[]; build: Build | null; onCreated: () => void }) {
  const capabilities = useCapabilities();
  const [models, setModels] = useState("");
  const [only, setOnly] = useState<string[]>([]);
  const [promptVersion, setPromptVersion] = useState("v2");
  const [hops, setHops] = useState(2);
  const [topK, setTopK] = useState(8);
  const [runTests, setRunTests] = useState(false);
  const toggleFamily = (f: string) => setOnly((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  const [pipelines, setPipelines] = useState<string[]>(["graphrag"]);
  const [judge, setJudge] = useState("");
  const [limit, setLimit] = useState("");
  const [concurrency, setConcurrency] = useState(4);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const availablePipelines = PIPELINES.filter((pipeline) => pipeline !== "vector" || Boolean(build?.stats?.embeddings?.embedding_model));
  useEffect(() => {
    if (capabilities.data?.default_model) setModels((current) => current || capabilities.data!.default_model);
  }, [capabilities.data?.default_model]);
  const toggle = (p: string) => setPipelines((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await post<Experiment[]>("/experiments", {
        dataset_id: datasetId, models: models.split(",").map((m) => m.trim()).filter(Boolean), pipelines,
        judge_model: judge.trim() || null, limit: limit ? Number(limit) : null, concurrency,
        families: only.length ? only : null, prompt_version: promptVersion.trim() || "v2", run_tests: runTests,
        options: { graphrag_hops: hops, top_k: topK },
      });
      onCreated();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="card" onSubmit={submit}>
      <h2>Compare AI answers with the graph&apos;s reference answers</h2>
      <p className="secondary">Start with one model and GraphRAG. For a fair comparison, select multiple pipelines to run the same model on the same questions. Real provider calls may incur usage costs.</p>
      <div className="row">
        <label className="field grow">Model names (comma-separated for more than one)
          <input type="text" required value={models} onChange={(e) => setModels(e.target.value)} style={{ minWidth: 420 }}
                 placeholder="Exact provider/model name, or mock/extractive for a demo" />
        </label>
      </div>
      <ModelStatus capabilities={capabilities.data} model={models} />
      <EmbeddingNote model={build?.stats?.embeddings?.embedding_model} />
      <div className="checks" style={{ margin: "12px 0" }}>
        {availablePipelines.map((p) => (
          <label key={p} title={PIPELINE_HELP[p]}><input type="checkbox" checked={pipelines.includes(p)} onChange={() => toggle(p)} /> {PIPELINE_LABEL[p]}</label>
        ))}
      </div>
      <button type="button" className="ghost" onClick={() => setPipelines([...availablePipelines])}>Compare all available pipelines</button>
      <p className="secondary">{pipelines.map((pipeline) => `${PIPELINE_LABEL[pipeline]}: ${PIPELINE_HELP[pipeline]}`).join(" ")}</p>
      <details><summary>Advanced experiment settings</summary>
      <div className="row" style={{ marginBottom: 12 }}>
        <label className="field">Judge model (optional, independent provider/model)
          <input type="text" value={judge} onChange={(e) => setJudge(e.target.value)} placeholder="Leave blank for graph checks" />
        </label>
        <label className="field">Limit questions<input type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} /></label>
        <label className="field">Concurrency<input type="number" min={1} max={32} value={concurrency}
                                                    onChange={(e) => setConcurrency(Number(e.target.value))} /></label>
        <label className="field">Prompt version<input type="text" value={promptVersion} style={{ minWidth: 80 }}
                                                      onChange={(e) => setPromptVersion(e.target.value)} /></label>
        <label className="field">GraphRAG hops
          <select value={hops} onChange={(e) => setHops(Number(e.target.value))}>
            {[1, 2, 3].map((h) => <option key={h} value={h}>{h}</option>)}
          </select>
        </label>
        <label className="field">Vector top-k<input type="number" min={1} max={50} value={topK}
                                                    onChange={(e) => setTopK(Number(e.target.value))} /></label>
      </div>
      {families.length > 1 && (
        <div className="checks" style={{ marginBottom: 12 }}>
          <span className="secondary">Only these families (none = all):</span>
          {families.map((f) => (
            <label key={f}><input type="checkbox" checked={only.includes(f)} onChange={() => toggleFamily(f)} /> {FAMILY_LABEL[f] ?? f}</label>))}
        </div>
      )}
      <div className="checks" style={{ marginBottom: 12 }}>
        <label><input type="checkbox" checked={runTests} onChange={(e) => setRunTests(e.target.checked)} />
          run code answers&apos; tests in the Docker sandbox (needs a Docker-capable worker)</label>
      </div>
      </details>
      <button style={{ marginTop: 12 }} disabled={busy || !models.trim() || !pipelines.length}>{busy ? "Starting…" : "Start evaluation"}</button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export default function DatasetPage() {
  const { id } = useParams<{ id: string }>();
  const datasetId = Number(id);
  const [family, setFamily] = useState("");
  const dataset = usePoll<Dataset>(`/datasets/${datasetId}`, 3000, (d) => isActive(d.status));
  const questions = usePoll<Question[]>(
    dataset.data?.status === "done" ? `/datasets/${datasetId}/questions${family ? `?family=${family}` : ""}` : null,
    0, () => false);
  const experiments = usePoll<Experiment[]>(`/experiments?dataset_id=${datasetId}`, 3000);
  const d = dataset.data;
  const build = usePoll<Build>(d ? `/builds/${d.build_id}` : null, 0, () => false);
  const counts = (questions.data ?? []).reduce<Record<string, number>>((acc, q) => ({ ...acc, [q.family]: (acc[q.family] ?? 0) + 1 }), {});

  return (
    <>
      <div className="spread">
        <div>
          <h1>Dataset #{datasetId}{d ? ` · ${d.version}` : ""}</h1>
          <p className="subtitle">
            {d && <>from <Link href={`/builds/${d.build_id}`}>build #{d.build_id}</Link> · {d.n_questions} questions · </>}
            <Link href={`/datasets/${datasetId}/leaderboard`}>Leaderboard →</Link>
          </p>
        </div>
        {d && <StatusBadge status={d.status} progress={d.progress} />}
      </div>
      {d?.status === "failed" && <pre className="card error">{d.error}</pre>}
      {dataset.error && <p className="error">{dataset.error}</p>}
      {d?.status === "done" && d.n_questions === 0 && <section className="card"><h2>No evaluation questions could be generated</h2><p>The selected question types require relevant files, functions, calls, tests or classes in the graph. <Link href={`/builds/${d.build_id}`}>Inspect this build&apos;s coverage</Link> and try file or definition questions, or choose another repository.</p></section>}

      {d?.status === "done" && d.n_questions > 0 && <ExperimentForm datasetId={datasetId} build={build.data} families={family ? [...FAMILIES] : Object.keys(counts).sort()}
                                               onCreated={experiments.refresh} />}

      <section className="card">
        <h2>Experiments</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Experiment</th><th>Model</th><th>Pipeline</th><th>Prompt</th><th>Status</th>
              <th className="num">F1</th><th className="num">Answers</th></tr></thead>
            <tbody>
              {(experiments.data ?? []).map((e) => (
                <tr key={e.id}>
                  <td><Link href={`/experiments/${e.id}`}>#{e.id}</Link></td>
                  <td className="mono">{e.model}{e.model.startsWith("mock/") && <span className="chip">Demo</span>}</td>
                  <td>{PIPELINE_LABEL[e.pipeline] ?? e.pipeline}</td>
                  <td>{e.prompt_version}</td>
                  <td><StatusBadge status={e.status} progress={e.progress} /></td>
                  <td className="num">{fmt(e.summary?.f1)}</td>
                  <td className="num">{e.summary?.n ?? "–"}</td>
                </tr>
              ))}
              {experiments.data?.length === 0 && <tr><td colSpan={7} className="muted">No experiments yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <div className="spread">
          <h2>Questions and reference answers</h2>
          <label className="field">Family
            <select value={family} onChange={(e) => setFamily(e.target.value)}>
              <option value="">all</option>
              {Object.keys(counts).length > 0 || family
                ? [...FAMILIES]
                  .map((f) => <option key={f} value={f}>{FAMILY_LABEL[f] ?? f}</option>) : null}
            </select>
          </label>
        </div>
        <p className="secondary">Reference answers come from the captured static graph and exclude name-only call guesses. They describe this commit and the graph&apos;s coverage. An empty reference set can be correct for a question containing a false assumption.</p>
        {questions.error && <p className="error">{questions.error}</p>}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Question type</th><th>Question</th><th>Reference answer</th><th className="num">Call depth</th></tr></thead>
            <tbody>
              {(questions.data ?? []).map((q) => (
                <tr key={q.id}>
                  <td>{FAMILY_LABEL[q.family] ?? q.family}</td>
                  <td style={{ maxWidth: 560 }}>
                    <pre style={{ fontFamily: "inherit", fontSize: 13 }}>{q.text.length > 700 ? `${q.text.slice(0, 700)}…` : q.text}</pre>
                    <details><summary>Graph query used to derive the reference</summary><pre className="mono">{q.gold_cypher}</pre></details>
                  </td>
                  <td className="mono" style={{ maxWidth: 360 }}>
                    {q.gold_answer.length ? q.gold_answer.join("\n") : <span className="muted">Empty reference set</span>}
                  </td>
                  <td className="num">{q.hop_count ?? "–"}</td>
                </tr>
              ))}
              {questions.data?.length === 0 && <tr><td colSpan={4} className="muted">No questions match this filter.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
