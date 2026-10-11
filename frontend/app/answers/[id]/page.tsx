"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useState } from "react";
import { Inline, VERDICT_ICON } from "@/components/Inline";
import { fmt, post } from "@/lib/api";
import { type AnswerChecks, PIPELINE_LABEL } from "@/lib/types";
import { usePoll } from "@/lib/usePoll";

const CodeView = dynamic(() => import("@/components/CodeView"), { ssr: false });


function Chip({ kind, text, invented = false }: { kind: "tp" | "fp" | "fn"; text: string; invented?: boolean }) {
  const meta = { tp: ["✓", "matches reference"], fp: ["✕", invented ? "not found in this graph" : "outside reference"], fn: ["○", "missed reference item"] }[kind];
  return <span className={`chip ${kind}`} title={meta[1]}><span className="icon" aria-hidden>{meta[0]}</span>
    <span className="mono">{text}</span><span className="muted">{meta[1]}</span></span>;
}

export default function AnswerPage() {
  const { id } = useParams<{ id: string }>();
  const checks = usePoll<AnswerChecks>(`/answers/${id}/checks`, 0, () => false);
  const [saved, setSaved] = useState<string | null>(null);
  const c = checks.data;
  if (checks.error) return <p className="error">{checks.error}</p>;
  if (!c) return <p className="muted">Loading…</p>;
  const buildId = c.build_id;
  const graphLink = (name: string) => (buildId ? `/builds/${buildId}?symbol=${encodeURIComponent(name)}#explore` : "#");
  const isCode = c.question.family === "codegen";

  async function label(value: string, claimId: number | null = null) {
    await post(`/answers/${id}/labels`, { label: value, claim_verdict_id: claimId });
    setSaved(`saved "${value}"${claimId ? ` for claim #${claimId}` : ""}`);
    checks.refresh();
  }

  return (
    <>
      <h1>Answer #{c.answer.id}</h1>
      <p className="subtitle">
        <span className="mono">{c.experiment.model}</span> × {PIPELINE_LABEL[c.experiment.pipeline] ?? c.experiment.pipeline} ·{" "}
        <Link href={`/experiments/${c.experiment.id}`}>experiment #{c.experiment.id}</Link> · {c.question.family}
        {c.question.hop_count ? ` · ${c.question.hop_count} hop(s)` : ""}
      </p>
      {c.experiment.model.startsWith("mock/") && <div className="notice"><p>This answer was produced by a demo model. Use a real model to evaluate AI understanding.</p></div>}

      <section className="card">
        <h2>Question</h2>
        <p style={{ whiteSpace: "pre-wrap" }}><Inline text={c.question.text} /></p>
        <h3>Reference answer from the static graph</h3>
        <div className="chips">
          {c.question.gold_answer.length
            ? c.question.gold_answer.map((g) => c.question.family === "files" ? <span key={g} className="chip mono">{g}</span>
              : <Link key={g} href={graphLink(g)} className="chip mono">{g}</Link>)
            : <span className="muted">Empty reference set{c.question.family === "false_premise" ? ": reject the false assumption" : ""}</span>}
        </div>
        <p className="secondary">Reference answers describe captured code relationships at this build&apos;s commit. Missing graph evidence can reflect incomplete static analysis, especially dynamic or cross-language calls.</p>
        <details style={{ marginTop: 8 }}><summary>Graph query behind the reference</summary><pre className="mono">{c.question.gold_cypher}</pre></details>
      </section>

      <section className="card">
        <div className="spread">
          <h2>{isCode ? "Generated code" : "Model answer"}</h2>
          <span className="secondary">
            F1 <strong>{fmt(c.scores.f1 ?? c.scores.callee_f1)}</strong> · {Math.round(c.answer.latency_ms)} ms · {c.answer.tokens} tokens
          </span>
        </div>
        {c.answer.error && <p className="error">{c.answer.error}</p>}
        {!isCode && (
          <>
            <div className="chips">
              {(c.answer.match.tp ?? []).map((x) => <Chip key={`tp${x}`} kind="tp" text={x} />)}
              {(c.answer.match.fp ?? []).map((x) => (
                <Chip key={`fp${x}`} kind="fp" text={x} invented={(c.answer.match.invented ?? []).includes(x)} />))}
              {(c.answer.match.fn ?? []).map((x) => <Chip key={`fn${x}`} kind="fn" text={x} />)}
            </div>
            {c.answer.explanation && <><h3>Explanation</h3><p><Inline text={c.answer.explanation} /></p></>}
          </>
        )}
        {isCode && c.answer.code && (
          <CodeView code={c.answer.code} height={360}
                    markers={c.symbols.filter((s) => s.category && s.line).map((s) => ({
                      line: s.line as number, message: `${s.category}: ${s.detail ?? s.symbol}`,
                      severity: s.category === "missing_symbol" || s.category === "syntax_error" ? "error" : "warning",
                    }))} />
        )}
        <details style={{ marginTop: 8 }}><summary>raw model output</summary><pre className="mono">{c.answer.text}</pre></details>
        {c.answer.steps?.length > 0 && (
          <details open><summary>Cypher agent queries ({c.answer.steps.length})</summary>
            {c.answer.steps.map((s, i) => (
              <div key={i} style={{ margin: "8px 0" }}>
                <pre className="mono">{s.cypher}</pre>
                <span className={s.error ? "error" : "muted"}>{s.error ?? `${s.rows} rows`}</span>
              </div>
            ))}
          </details>
        )}
        <div className="row" style={{ marginTop: 12 }}>
          <span className="secondary">Human label for this answer:</span>
          {isCode
            ? (["clean", "hallucinated"] as const).map((v) => <button key={v} className="ghost" onClick={() => label(v)}>{v}</button>)
            : (["correct", "incorrect"] as const).map((v) => <button key={v} className="ghost" onClick={() => label(v)}>{v}</button>)}
          {saved && <span className="muted">{saved}</span>}
        </div>
      </section>

      {c.claims.length > 0 && (
        <section className="card">
          <h2>Claims checked against the graph</h2>
          <p className="secondary">Supported means captured evidence agrees. Contradicted means the claim conflicts with graph evidence. Unverifiable means the graph cannot decide. These labels apply to extracted claims; they do not verify the whole explanation.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Claim</th><th>Verdict</th><th>Evidence</th><th>Your label</th></tr></thead>
              <tbody>
                {c.claims.map((cl) => (
                  <tr key={cl.id}>
                    <td>
                      <Link className="mono" href={graphLink(cl.subject)}>{cl.subject}</Link>{" "}
                      <span className="secondary">{cl.relation.replace("_", " ")}</span>{" "}
                      <Link className="mono" href={graphLink(cl.object)}>{cl.object}</Link>
                      <div className="muted">{cl.extractor} extractor</div>
                    </td>
                    <td><span className={`verdict ${cl.verdict}`}>{VERDICT_ICON[cl.verdict]} {cl.verdict}</span></td>
                    <td className="mono" style={{ maxWidth: 520, wordBreak: "break-all" }}>{cl.evidence_edge}</td>
                    <td>
                      <select defaultValue="" onChange={(e) => e.target.value && label(e.target.value, cl.id)}>
                        <option value="">–</option><option>supported</option><option>contradicted</option><option>unverifiable</option>
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {isCode && c.symbols.length > 0 && (
        <section className="card">
          <h2>Symbol checks</h2>
          <table>
            <thead><tr><th>Symbol</th><th>Use</th><th>Result</th><th>Detail</th><th className="num">Line</th></tr></thead>
            <tbody>
              {c.symbols.map((s, i) => (
                <tr key={i}>
                  <td className="mono">{s.symbol}</td>
                  <td>{s.kind}</td>
                  <td><span className={`verdict ${s.category ? "contradicted" : "supported"}`}>
                    {s.category ? `✕ ${s.category.replace("_", " ")}` : "✓ ok"}</span></td>
                  <td>{s.detail}</td>
                  <td className="num">{s.line ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {c.test_runs.map((t, i) => (
            <p key={i} className="secondary">Sandbox: {t.status} · {t.passed}/{t.tests_run} tests passed</p>
          ))}
        </section>
      )}

      <section className="card">
        <h2>Scores</h2>
        <dl className="kv">
          {Object.entries(c.scores).sort().map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{fmt(v)}</dd></Fragment>))}
        </dl>
        {c.answer.retrieved_nodes.length > 0 && (
          <details style={{ marginTop: 8 }}><summary>retrieved nodes ({c.answer.retrieved_nodes.length})</summary>
            <pre className="mono">{c.answer.retrieved_nodes.join("\n")}</pre></details>
        )}
        {c.answer.trace_id && <p className="muted">trace id <span className="mono">{c.answer.trace_id}</span></p>}
      </section>
    </>
  );
}
