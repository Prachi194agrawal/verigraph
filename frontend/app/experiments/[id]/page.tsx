"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import StatusBadge from "@/components/StatusBadge";
import { fmt, pct, post } from "@/lib/api";
import { type AnswerRow, type Experiment, PIPELINE_LABEL, type Summary } from "@/lib/types";
import { isActive, usePoll } from "@/lib/usePoll";

interface Results {
  experiment: Experiment;
  summary: Summary;
  answers: AnswerRow[];
}

export default function ExperimentPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [family, setFamily] = useState("");
  const res = usePoll<Results>(`/experiments/${id}/results`, 3000, (r) => isActive(r.experiment.status));
  const r = res.data;
  const [resumeError, setResumeError] = useState<string | null>(null);
  async function resume() {
    setResumeError(null);
    try {
      await post(`/experiments/${id}/resume`, {});
      res.refresh();
    } catch (err) {
      setResumeError(String(err));
    }
  }
  const answers = (r?.answers ?? []).filter((a) => !family || a.family === family);
  const families = [...new Set((r?.answers ?? []).map((a) => a.family))].sort();

  return (
    <>
      <div className="spread">
        <div>
          <h1>Experiment #{id}</h1>
          {r && (
            <p className="subtitle">
              <span className="mono">{r.experiment.model}</span> × {PIPELINE_LABEL[r.experiment.pipeline] ?? r.experiment.pipeline}
              {" "}· prompt {r.experiment.prompt_version} ·{" "}
              <Link href={`/datasets/${r.experiment.dataset_id}/leaderboard`}>leaderboard →</Link>
            </p>
          )}
        </div>
        {r && <StatusBadge status={r.experiment.status} progress={r.experiment.progress} />}
      </div>
      {r?.experiment.status === "failed" && (
        <section className="card">
          <div className="spread">
            <h2>This experiment failed</h2>
            <button onClick={resume}>Resume</button>
          </div>
          <p className="muted">Resuming re-queues it; questions that already have an answer are skipped.</p>
          {resumeError && <p className="error">{resumeError}</p>}
          <pre className="error">{r.experiment.error}</pre>
        </section>
      )}
      {r?.experiment.status === "done" && r.summary.errors > 0 && (
        <section className="card">
          <div className="spread">
            <h2>{r.summary.errors} model call{r.summary.errors === 1 ? "" : "s"} failed</h2>
            <button onClick={resume}>Retry failed calls</button>
          </div>
          <p className="muted">
            Rate limits, timeouts and provider outages are not wrong answers. Until they are retried they count as
            F1 0 in the mean; F1 over the answered questions is {fmt(r.summary.f1_answered)}. Retrying asks only
            these questions again.
          </p>
          {resumeError && <p className="error">{resumeError}</p>}
        </section>
      )}
      {r && (
        <div className="tiles">
          <div className="tile"><div className="label">Mean F1</div><div className="value">{fmt(r.summary.f1)}</div>
            <div className="note">95% CI {fmt(r.summary.f1_ci?.[0], 2)}–{fmt(r.summary.f1_ci?.[1], 2)}</div></div>
          <div className="tile"><div className="label">Context recall</div><div className="value">{fmt(r.summary.context_recall)}</div>
            <div className="note">gold items the retrieval showed</div></div>
          <div className="tile"><div className="label">Contradicted claims</div><div className="value">{pct(r.summary.contradicted_claim_rate)}</div>
            <div className="note">of extracted claims</div></div>
          <div className="tile"><div className="label">Answers</div><div className="value">{r.summary.n}</div>
            <div className="note">{r.summary.errors} errors</div></div>
          <div className="tile"><div className="label">Cost per question</div>
            <div className="value">{r.summary.cost_per_question === null ? "–" : `$${r.summary.cost_per_question.toFixed(4)}`}</div></div>
          <div className="tile"><div className="label">Latency</div>
            <div className="value">{r.summary.latency_ms_median === null ? "–" : `${Math.round(r.summary.latency_ms_median)} ms`}</div>
            <div className="note">median per question</div></div>
        </div>
      )}
      <section className="card">
        <div className="spread">
          <h2>Per-question scores</h2>
          <label className="field">Family
            <select value={family} onChange={(e) => setFamily(e.target.value)}>
              <option value="">all</option>
              {families.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </label>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Answer</th><th>Family</th><th className="num">Hops</th><th className="num">F1</th>
              <th className="num">Precision</th><th className="num">Recall</th><th className="num">Ctx recall</th>
              <th className="num">Contradicted</th><th className="num">ms</th><th>Error</th></tr></thead>
            <tbody>
              {answers.map((a) => (
                <tr key={a.answer_id} className="clickable" onClick={() => router.push(`/answers/${a.answer_id}`)}>
                  <td><Link href={`/answers/${a.answer_id}`}>#{a.answer_id}</Link></td>
                  <td>{a.family}</td>
                  <td className="num">{a.hop_count ?? "–"}</td>
                  <td className="num"><strong>{fmt(a.scores.f1 ?? a.scores.callee_f1)}</strong></td>
                  <td className="num">{fmt(a.scores.precision)}</td>
                  <td className="num">{fmt(a.scores.recall)}</td>
                  <td className="num">{fmt(a.scores.context_recall)}</td>
                  <td className="num">{pct(a.scores.contradicted_claim_rate)}</td>
                  <td className="num">{Math.round(a.latency_ms)}</td>
                  <td className="error">{a.error ? a.error.slice(0, 80) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
