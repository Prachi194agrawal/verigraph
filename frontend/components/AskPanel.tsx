"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ModelStatus, useCapabilities } from "@/components/SetupStatus";
import { Inline, VERDICT_ICON } from "@/components/Inline";
import { post } from "@/lib/api";
import { type AskResult, PIPELINE_HELP, PIPELINE_LABEL, PIPELINES } from "@/lib/types";

/** Ask the graph a free-form question; the answer's names and claims are checked against the graph. */
export default function AskPanel({ buildId, hasEmbeddings }: { buildId: number; hasEmbeddings: boolean }) {
  const capabilities = useCapabilities();
  const [question, setQuestion] = useState("");
  const [model, setModel] = useState("");
  const [pipeline, setPipeline] = useState("graphrag");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResult | null>(null);
  useEffect(() => {
    if (capabilities.data?.default_model) setModel((current) => current || capabilities.data!.default_model);
  }, [capabilities.data?.default_model]);
  const graphLink = (name: string) => `/builds/${buildId}?symbol=${encodeURIComponent(name)}#explore`;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await post<AskResult>(`/builds/${buildId}/ask`, { question, model: model.trim(), pipeline }));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" id="ask">
      <h2>Ask about the repository with an AI model</h2>
      <p className="muted">
        Ask where a function is used or which tests may be affected. The graph checks returned names and extracted
        relationships. A supported claim matches captured code evidence; this check cannot verify every sentence or runtime behavior.
      </p>
      <form onSubmit={submit}>
        <label className="field">Question
          <textarea rows={2} required value={question} onChange={(e) => setQuestion(e.target.value)}
                    placeholder="Which functions call [paste a function name from the explorer]?" />
        </label>
        <div className="row" style={{ margin: "12px 0" }}>
          <label className="field">Model
            <input type="text" required value={model} onChange={(e) => setModel(e.target.value)}
                   placeholder="Exact provider/model name, or mock/extractive for a demo" />
          </label>
          <label className="field">How to find context
            <select value={pipeline} onChange={(e) => setPipeline(e.target.value)}>
              {PIPELINES.filter((p) => p !== "vector" || hasEmbeddings).map((p) => (
                <option key={p} value={p}>{PIPELINE_LABEL[p]}</option>))}
            </select>
          </label>
        </div>
        <p className="secondary">{PIPELINE_HELP[pipeline]}</p>
        <ModelStatus capabilities={capabilities.data} model={model} />
        <button disabled={busy || !question.trim() || !model.trim()}>{busy ? "Asking…" : "Ask"}</button>
        {error && <p className="error">{error}</p>}
      </form>

      {result && (
        <div style={{ marginTop: 16 }}>
          {result.error && <p className="error">{result.error}</p>}
          {result.provenance?.model_kind === "mock" && <div className="notice"><p>This answer came from a demo model. Use a real model to get AI explanations.</p></div>}
          {result.provenance?.commit_sha && <p className="secondary">Evidence from commit <code>{result.provenance.commit_sha.slice(0, 12)}</code>.</p>}
          <h3>Answer</h3>
          <div className="chips">
            {result.answer.length === 0 && <span className="muted">empty list</span>}
            {result.answer.map((item) => {
              const [icon, note, kind] = item.in_graph === true ? ["✓", item.kind ?? "in graph", "tp"]
                : item.in_graph === false ? ["?", "not found in this graph", "fp"] : ["·", "", ""];
              const body = <><span className="icon" aria-hidden>{icon}</span><span className="mono">{item.name}</span>
                <span className="muted">{note}</span></>;
              return item.in_graph && item.kind !== "file"
                ? <Link key={item.raw} className={`chip ${kind}`} href={graphLink(item.name)}>{body}</Link>
                : <span key={item.raw} className={`chip ${kind}`}>{body}</span>;
            })}
          </div>
          {result.explanation && <p><Inline text={result.explanation} /></p>}
          <p className="secondary">
            {result.summary.supported} supported · {result.summary.contradicted} contradicted ·{" "}
            {result.summary.unverifiable} unverifiable claims · {Math.round(result.latency_ms)} ms ·{" "}
            {result.prompt_tokens + result.completion_tokens} tokens
          </p>
          {result.limitations?.map((limitation) => <p className="secondary" key={limitation}>{limitation}</p>)}
          {result.claims.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Claim</th><th>Verdict</th><th>Evidence</th></tr></thead>
                <tbody>
                  {result.claims.map((c, i) => (
                    <tr key={i}>
                      <td><span className="mono">{c.subject}</span>{" "}
                        <span className="secondary">{c.relation.replace("_", " ")}</span>{" "}
                        <span className="mono">{c.object}</span></td>
                      <td><span className={`verdict ${c.verdict}`}>{VERDICT_ICON[c.verdict]} {c.verdict}</span></td>
                      <td className="mono" style={{ maxWidth: 520, wordBreak: "break-all" }}>{c.evidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.steps.length > 0 && (
            <details><summary>Cypher agent queries ({result.steps.length})</summary>
              {result.steps.map((s, i) => (
                <div key={i} style={{ margin: "8px 0" }}>
                  <pre className="mono">{s.cypher}</pre>
                  <span className={s.error ? "error" : "muted"}>{s.error ?? `${s.rows} rows`}</span>
                </div>
              ))}
            </details>
          )}
          {result.context && (
            <details><summary>context shown to the model</summary><pre className="mono">{result.context}</pre></details>
          )}
        </div>
      )}
    </section>
  );
}
