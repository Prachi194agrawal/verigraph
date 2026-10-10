"use client";

import Link from "next/link";
import { useState } from "react";
import { post } from "@/lib/api";
import type { ImpactResult } from "@/lib/types";

/** Which functions and tests a change can affect: the precise call graph, walked backwards. No LLM. */
export default function ImpactPanel({ buildId }: { buildId: number }) {
  const [functions, setFunctions] = useState("");
  const [diff, setDiff] = useState("");
  const [hops, setHops] = useState(3);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImpactResult | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const graphLink = (name: string) => `/builds/${buildId}?symbol=${encodeURIComponent(name)}`;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await post<ImpactResult>(`/builds/${buildId}/impact`, {
        functions: functions.split(/[\s,]+/).map((f) => f.trim()).filter(Boolean), diff: diff.trim() || null,
        max_hops: hops,
      }));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function copy(label: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
    } catch {
      setCopied("copy failed: select the text instead");
    }
  }

  const tests = (result?.tests ?? []).map((t) => t.pytest_id ?? t.qualified_name);
  return (
    <section className="card">
      <h2>Change impact</h2>
      <p className="muted">
        Name changed functions or paste a unified diff of this build&apos;s commit (a pull request&apos;s base). Precise
        CALLS edges are followed backwards; name-matched callers are listed apart because those edges are guesses.
      </p>
      <form onSubmit={submit}>
        <div className="row">
          <label className="field">Changed functions (comma-separated)
            <input type="text" value={functions} onChange={(e) => setFunctions(e.target.value)}
                   placeholder="Session.send, utils.super_len" style={{ minWidth: 360 }} />
          </label>
          <label className="field">Hops
            <select value={hops} onChange={(e) => setHops(Number(e.target.value))}>
              {[1, 2, 3, 4, 5].map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </label>
        </div>
        <label className="field" style={{ marginTop: 12 }}>Unified diff (optional)
          <textarea rows={4} value={diff} onChange={(e) => setDiff(e.target.value)} className="mono"
                    placeholder={"--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -10,3 +10,3 @@ ..."} />
        </label>
        <button style={{ marginTop: 12 }} disabled={busy || (!functions.trim() && !diff.trim())}>
          {busy ? "Analysing…" : "Analyse impact"}
        </button>
        {error && <p className="error">{error}</p>}
      </form>

      {result && (
        <div style={{ marginTop: 16 }}>
          <h3>Changed ({result.changed.length})</h3>
          <div className="chips">
            {result.changed.map((c) => (
              <Link key={c.qualified_name} className="chip mono" href={graphLink(c.qualified_name)}>{c.qualified_name}</Link>))}
            {result.changed.length === 0 && <span className="muted">no function of this build</span>}
          </div>
          {result.unmatched.length > 0 && <p className="muted">Not mapped: {result.unmatched.join("; ")}</p>}
          <div className="grid-2">
            <div>
              <h3>Affected callers ({result.affected.length})</h3>
              <table>
                <thead><tr><th>Function</th><th className="num">Hops</th></tr></thead>
                <tbody>
                  {result.affected.map((a) => (
                    <tr key={a.qualified_name}>
                      <td><Link className="mono" href={graphLink(a.qualified_name)}>{a.qualified_name}</Link></td>
                      <td className="num">{a.hops}</td>
                    </tr>
                  ))}
                  {result.affected.length === 0 && <tr><td colSpan={2} className="muted">none</td></tr>}
                </tbody>
              </table>
            </div>
            <div>
              <div className="spread">
                <h3>Tests to run ({tests.length})</h3>
                {tests.length > 0 && <button className="ghost" onClick={() => copy("pytest ids", tests.join(" "))}>Copy pytest ids</button>}
              </div>
              <pre className="mono">{tests.length ? tests.join("\n") : "no test reaches the change through precise edges"}</pre>
            </div>
          </div>
          {result.possibly_affected.length > 0 && (
            <p className="secondary">Possibly affected through name-matched edges:{" "}
              {result.possibly_affected.map((p) => p.qualified_name).join(", ")}</p>
          )}
          <details><summary>Markdown for a pull-request comment</summary>
            <button className="ghost" style={{ margin: "8px 0" }} onClick={() => copy("Markdown", result.markdown)}>Copy Markdown</button>
            <pre className="mono">{result.markdown}</pre>
          </details>
          {copied && <p className="muted">{copied.startsWith("copy failed") ? copied : `Copied ${copied}.`}</p>}
        </div>
      )}
    </section>
  );
}
