"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import StatusBadge from "@/components/StatusBadge";
import { fmt, post } from "@/lib/api";
import { type Dataset, type Experiment, FAMILIES, PIPELINE_LABEL, PIPELINES, type Question } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

function ExperimentForm({ datasetId, families, onCreated }: { datasetId: number; families: string[]; onCreated: () => void }) {
  const [models, setModels] = useState("");
  const [only, setOnly] = useState<string[]>([]);
  const [promptVersion, setPromptVersion] = useState("v1");
  const [hops, setHops] = useState(2);
  const [topK, setTopK] = useState(8);
  const [runTests, setRunTests] = useState(false);
  const toggleFamily = (f: string) => setOnly((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  const [pipelines, setPipelines] = useState<string[]>([...PIPELINES]);
  const [judge, setJudge] = useState("");
  const [limit, setLimit] = useState("");
  const [concurrency, setConcurrency] = useState(4);
  const [error, setError] = useState<string | null>(null);
  const toggle = (p: string) => setPipelines((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await post<Experiment[]>("/experiments", {
        dataset_id: datasetId, models: models.split(",").map((m) => m.trim()).filter(Boolean), pipelines,
        judge_model: judge.trim() || null, limit: limit ? Number(limit) : null, concurrency,
        families: only.length ? only : null, prompt_version: promptVersion.trim() || "v1", run_tests: runTests,
        options: { graphrag_hops: hops, top_k: topK },
      });
      onCreated();
    } catch (err) {
      setError(String(err));
    }
  }
  return (
    <form className="card" onSubmit={submit}>
      <h2>Run models × pipelines (steps 5–6)</h2>
      <div className="row">
        <label className="field">Models (LiteLLM names, comma-separated)
          <input type="text" required value={models} onChange={(e) => setModels(e.target.value)} style={{ minWidth: 420 }}
                 placeholder="groq/llama-3.1-8b-instant, gemini/gemini-2.0-flash, ollama/qwen2.5-coder:7b" />
        </label>
        <label className="field">Judge model (optional, other family)
          <input type="text" value={judge} onChange={(e) => setJudge(e.target.value)} placeholder="gemini/gemini-2.0-flash" />
        </label>
      </div>
      <div className="checks" style={{ margin: "12px 0" }}>
        {PIPELINES.map((p) => (
          <label key={p}><input type="checkbox" checked={pipelines.includes(p)} onChange={() => toggle(p)} /> {PIPELINE_LABEL[p]}</label>
        ))}
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
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
            <label key={f}><input type="checkbox" checked={only.includes(f)} onChange={() => toggleFamily(f)} /> {f}</label>))}
        </div>
      )}
      <div className="checks" style={{ marginBottom: 12 }}>
        <label><input type="checkbox" checked={runTests} onChange={(e) => setRunTests(e.target.checked)} />
          run code answers&apos; tests in the Docker sandbox (needs a Docker-capable worker)</label>
      </div>
      <button disabled={!models.trim() || !pipelines.length}>Start experiments</button>
      <p className="muted">Offline test doubles: <span className="mono">mock/extractive</span>, <span className="mono">mock/empty</span>.</p>
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

      {d?.status === "done" && <ExperimentForm datasetId={datasetId} families={family ? [...FAMILIES] : Object.keys(counts).sort()}
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
                  <td className="mono">{e.model}</td>
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
          <h2>Questions</h2>
          <label className="field">Family
            <select value={family} onChange={(e) => setFamily(e.target.value)}>
              <option value="">all</option>
              {Object.keys(counts).length > 0 || family
                ? ["lookup", "callers", "impact", "inheritance", "history", "localization", "false_premise", "codegen"]
                  .map((f) => <option key={f} value={f}>{f}</option>) : null}
            </select>
          </label>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Family</th><th>Question</th><th>Gold answer</th><th className="num">Hops</th></tr></thead>
            <tbody>
              {(questions.data ?? []).map((q) => (
                <tr key={q.id}>
                  <td>{q.family}</td>
                  <td style={{ maxWidth: 560 }}>
                    <pre style={{ fontFamily: "inherit", fontSize: 13 }}>{q.text.length > 700 ? `${q.text.slice(0, 700)}…` : q.text}</pre>
                    <details><summary>gold Cypher</summary><pre className="mono">{q.gold_cypher}</pre></details>
                  </td>
                  <td className="mono" style={{ maxWidth: 360 }}>
                    {q.gold_answer.length ? q.gold_answer.join("\n") : <span className="muted">[] (false premise)</span>}
                  </td>
                  <td className="num">{q.hop_count ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
