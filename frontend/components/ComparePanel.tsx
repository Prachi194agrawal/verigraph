"use client";

import { useEffect, useState } from "react";
import { api, fmt } from "@/lib/api";
import { type LeaderRow, PIPELINE_LABEL } from "@/lib/types";
import { usePoll } from "@/lib/usePoll";

interface Comparison { metric: string; a: number; b: number; diff: number | null; lo: number | null; hi: number | null;
  n: number; p: number | null; excluded_failed_calls?: number }
interface Agreement { n: number; kappa: number | null; raw_agreement: number | null }

const label = (r: LeaderRow) => `#${r.experiment_id} ${r.model} × ${PIPELINE_LABEL[r.pipeline] ?? r.pipeline}`;

/** Is a difference between two experiments real? Paired bootstrap over the questions both answered. */
export function ComparePanel({ rows }: { rows: LeaderRow[] }) {
  const [a, setA] = useState<number | null>(null);
  const [b, setB] = useState<number | null>(null);
  const [metric, setMetric] = useState("f1");
  const [result, setResult] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (rows.length >= 2 && a === null && b === null) {
      setA(rows[0].experiment_id);
      setB(rows[1].experiment_id);
    }
  }, [rows, a, b]);
  useEffect(() => {
    if (a === null || b === null || a === b) return;
    api<Comparison>(`/compare?a=${a}&b=${b}&metric=${metric}`).then((r) => { setResult(r); setError(null); })
      .catch((e) => setError(String(e)));
  }, [a, b, metric]);
  if (rows.length < 2) return null;
  const significant = result?.lo != null && result?.hi != null && (result.lo > 0 || result.hi < 0);
  return (
    <section className="card">
      <h2>Compare two experiments</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        {([["A", a, setA], ["B", b, setB]] as const).map(([name, value, set]) => (
          <label key={name} className="field">{name}
            <select value={value ?? ""} onChange={(e) => set(Number(e.target.value))}>
              {rows.map((r) => <option key={r.experiment_id} value={r.experiment_id}>{label(r)}</option>)}
            </select>
          </label>
        ))}
        <label className="field">Metric
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            {["f1", "precision", "recall", "context_recall", "claim_precision", "callee_f1", "hallucinated"].map((m) => (
              <option key={m} value={m}>{m}</option>))}
          </select>
        </label>
      </div>
      {a === b && <p className="muted">Pick two different experiments.</p>}
      {error && <p className="error">{error}</p>}
      {result && a !== b && (
        result.n === 0 ? <p className="muted">These experiments share no answered questions.</p> : (
          <p>
            mean {metric} difference A − B = <strong>{fmt(result.diff)}</strong>{" "}
            <span className="muted">[95% CI {fmt(result.lo)}, {fmt(result.hi)}] over {result.n} shared questions,
              p ≈ {fmt(result.p, 3)}
              {result.excluded_failed_calls ? ` (${result.excluded_failed_calls} left out: a model call failed)` : ""}</span>
            <br />
            <span className="secondary">{significant
              ? "The interval excludes 0: the difference is unlikely to be noise."
              : "The interval includes 0: this difference could be noise."}</span>
          </p>
        )
      )}
    </section>
  );
}

/** E6: do the automatic checkers agree with human labels? Cohen's kappa from the labels given in answer views. */
export function AgreementPanel({ datasetId }: { datasetId: number }) {
  const agreement = usePoll<Record<string, Agreement>>(`/datasets/${datasetId}/agreement`, 15000);
  const rows = Object.entries(agreement.data ?? {});
  const names: Record<string, string> = {
    claim_checker: "Claim checker (per claim)", set_f1_exact_match: "Set-F1 exact match (per answer)",
    symbol_checker: "Symbol checker (per code answer)",
  };
  return (
    <section className="card">
      <h2>Checker agreement with human labels (E6)</h2>
      <p className="muted">Label answers and claims on the answer pages; kappa needs at least two labels per checker.</p>
      <table>
        <thead><tr><th>Checker</th><th className="num">Labels</th><th className="num">Cohen&apos;s kappa</th>
          <th className="num">Raw agreement</th></tr></thead>
        <tbody>
          {rows.map(([key, v]) => (
            <tr key={key}>
              <td>{names[key] ?? key}</td><td className="num">{v.n}</td>
              <td className="num">{fmt(v.kappa)}</td><td className="num">{fmt(v.raw_agreement)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
