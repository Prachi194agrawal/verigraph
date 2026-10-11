"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { FamilyChart, HopChart } from "@/components/Charts";
import { AgreementPanel, ComparePanel } from "@/components/ComparePanel";
import StatusBadge from "@/components/StatusBadge";
import { fmt, pct } from "@/lib/api";
import { FAMILY_LABEL, type HopPoint, type LeaderRow, PIPELINE_LABEL, PIPELINES } from "@/lib/types";
import { usePoll } from "@/lib/usePoll";

export default function LeaderboardPage() {
  const { id } = useParams<{ id: string }>();
  const datasetId = Number(id);
  const board = usePoll<LeaderRow[]>(`/leaderboard?dataset_id=${datasetId}`, 5000);
  const hops = usePoll<HopPoint[]>(`/leaderboard/hops?dataset_id=${datasetId}`, 5000);
  const rows = board.data ?? [];
  const models = useMemo(() => [...new Set(rows.map((r) => r.model))].sort(), [rows]);
  const [model, setModel] = useState<string>("");
  useEffect(() => {
    if (!model && models.length) setModel(models[0]);
  }, [models, model]);

  const modelRows = rows.filter((r) => r.model === model);
  const pipelines = PIPELINES.filter((p) => modelRows.some((r) => r.pipeline === p));
  const families = [...new Set(rows.flatMap((r) => Object.keys(r.f1_by_family ?? {})))].sort();
  const hopKeys = [...new Set(rows.flatMap((r) => Object.keys(r.f1_by_hop ?? {})))].sort();

  return (
    <>
      <h1>Leaderboard · dataset #{datasetId}</h1>
      <p className="subtitle">
        Compare model answers on the same repository questions. F1 measures agreement with graph-derived reference answers, from 0 to 1.{" "}
        <Link href={`/datasets/${datasetId}`}>Questions &amp; experiments →</Link>
      </p>
      <section className="notice">
        <p><strong>Read these scores as AI answer accuracy.</strong> They do not rate repository quality, security or runtime correctness. The static graph may miss dynamic or cross-language relationships.</p>
        {rows.some((row) => row.model.startsWith("mock/")) && <p><strong>Demo models are included.</strong> Names starting with <code>mock/</code> verify the workflow. Their scores are not real model benchmark results. <code>mock/empty</code> can score well on questions whose correct answer is empty.</p>}
      </section>
      {board.error && <p className="error">{board.error}</p>}
      <details className="card"><summary>Understand the metrics</summary>
        <dl className="kv">
          <dt>F1</dt><dd>Balances precision (returned items that match the reference) and recall (reference items found). 1 means the answer set matches.</dd>
          <dt>95% interval</dt><dd>An uncertainty estimate from resampling these questions. Small datasets give less reliable comparisons.</dd>
          <dt>Context recall</dt><dd>The share of reference answer items included in retrieved context.</dd>
          <dt>Claim precision</dt><dd>Supported claims divided by supported plus contradicted claims; unverifiable claims are excluded.</dd>
          <dt>Contradicted claims</dt><dd>Extracted relationships that conflict with captured graph evidence.</dd>
          <dt>Call depth</dt><dd>Accuracy on affected-test questions requiring chains of 1, 2 or 3 function calls.</dd>
          <dt>Failed calls</dt><dd>Provider errors or timeouts. Check the experiment and retry them before comparing answer accuracy.</dd>
        </dl>
      </details>

      {/* One filter row above everything it scopes */}
      <div className="row" style={{ marginBottom: 16 }}>
        <label className="field">Model shown in the charts
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => <option key={m} value={m}>{m}{m.startsWith("mock/") ? " (demo)" : ""}</option>)}
          </select>
        </label>
      </div>

      {rows.length > 0 && <div className="grid-2">
        <section className="card">
          <h2>Accuracy by question type</h2>
          <FamilyChart rows={modelRows} pipelines={pipelines} />
        </section>
        <section className="card">
          <h2>Accuracy by call chain depth</h2>
          <HopChart points={(hops.data ?? []).filter((p) => p.model === model)} pipelines={pipelines} />
        </section>
      </div>}

      <section className="card">
        <h2>All experiments</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Model</th><th>Pipeline</th><th>Status</th><th className="num">F1 [95% CI]</th>
                {families.map((f) => <th key={f} className="num">{FAMILY_LABEL[f] ?? f.replace("_", " ")}</th>)}
                {hopKeys.map((h) => <th key={h} className="num">{h} hop</th>)}
                <th className="num">context recall</th><th className="num">claim precision</th><th className="num">contradicted</th>
                <th className="num">premise rejected</th><th className="num">hit@5</th>
                <th className="num">code check issues</th><th className="num">unrecognized symbols</th>
                <th className="num">$/q</th><th className="num">ms/q</th><th className="num">n</th>
                <th className="num">failed calls</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.experiment_id}>
                  <td className="mono"><Link href={`/experiments/${r.experiment_id}`}>{r.model}</Link>{r.model.startsWith("mock/") && <span className="chip">Demo</span>}</td>
                  <td style={{ whiteSpace: "nowrap" }}>{PIPELINE_LABEL[r.pipeline] ?? r.pipeline}</td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="num"><strong>{fmt(r.f1)}</strong> <span className="muted">[{fmt(r.f1_ci?.[0], 2)}, {fmt(r.f1_ci?.[1], 2)}]</span></td>
                  {families.map((f) => <td key={f} className="num">{fmt(r.f1_by_family?.[f])}</td>)}
                  {hopKeys.map((h) => <td key={h} className="num">{fmt(r.f1_by_hop?.[h])}</td>)}
                  <td className="num">{fmt(r.context_recall)}</td>
                  <td className="num">{fmt(r.claim_precision)}</td>
                  <td className="num">{pct(r.contradicted_claim_rate)}</td>
                  <td className="num">{pct(r.false_premise_rejection_rate)}</td>
                  <td className="num">{fmt(r.hit_at_5)}</td>
                  <td className="num">{pct(r.codegen?.hallucinated)}</td>
                  <td className="num">{pct(r.codegen?.missing_symbol_rate)}</td>
                  <td className="num">{r.cost_per_question === null ? "–" : `$${r.cost_per_question.toFixed(4)}`}</td>
                  <td className="num">{r.latency_ms_mean === null ? "–" : Math.round(r.latency_ms_mean)}</td>
                  <td className="num">{r.n}</td>
                  <td className="num">{r.errors ? <span title="F1 over the questions that were answered">
                    {r.errors} <span className="muted">(F1 answered {fmt(r.f1_answered)})</span></span> : 0}</td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={20} className="muted">No experiments yet. <Link href={`/datasets/${datasetId}`}>Choose a model and start an evaluation.</Link></td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <ComparePanel rows={rows.filter((r) => r.status === "done")} />
      <AgreementPanel datasetId={datasetId} />
    </>
  );
}
