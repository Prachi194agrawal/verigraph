"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { GraphEdge, GraphNode } from "@/lib/types";

const CallGraph = dynamic(() => import("./CallGraph"), { ssr: false });
const CodeView = dynamic(() => import("./CodeView"), { ssr: false });

interface SymbolHit {
  qualified_name: string;
  kind: string;
  file_path: string;
  start_line: number;
  end_line: number;
  is_test: boolean;
}

interface FileView {
  path: string;
  content: string;
}

export default function GraphExplorer({ buildId, initialSymbol }: { buildId: number; initialSymbol?: string | null }) {
  const [query, setQuery] = useState(initialSymbol ?? "");
  const [hits, setHits] = useState<SymbolHit[]>([]);
  const [focus, setFocus] = useState<string | null>(initialSymbol ?? null);
  const [depth, setDepth] = useState(1);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [details, setDetails] = useState<Record<string, any> | null>(null);
  const [file, setFile] = useState<FileView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (query.trim().length < 2 || query === focus) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      api<SymbolHit[]>(`/builds/${buildId}/symbols?q=${encodeURIComponent(query.trim())}&limit=25`)
        .then(setHits).catch((e) => setError(String(e)));
    }, 200);
    return () => clearTimeout(t);
  }, [query, buildId, focus]);

  useEffect(() => {
    if (!focus) return;
    setError(null);
    const qn = encodeURIComponent(focus);
    api<{ nodes: GraphNode[]; edges: GraphEdge[] }>(`/builds/${buildId}/neighborhood?qn=${qn}&depth=${depth}`)
      .then(setGraph).catch((e) => setError(String(e)));
    api<Record<string, any>>(`/builds/${buildId}/node?qn=${qn}`)
      .then((d) => {
        setDetails(d);
        return api<FileView>(`/builds/${buildId}/file?path=${encodeURIComponent(d.file_path)}`);
      })
      .then(setFile)
      .catch((e) => setError(String(e)));
  }, [focus, depth, buildId]);

  const choose = useCallback((qn: string) => {
    setFocus(qn);
    setQuery(qn);
    setHits([]);
  }, []);

  return (
    <div className="card">
      <div className="spread">
        <h2>Call-graph explorer</h2>
        <div className="row">
          <label className="field">
            Depth
            <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
              {[1, 2, 3].map((d) => <option key={d} value={d}>{d} hop{d > 1 ? "s" : ""}</option>)}
            </select>
          </label>
        </div>
      </div>
      <label className="field">
        Find a function or class
        <input type="text" value={query} placeholder="e.g. Session.send" onChange={(e) => setQuery(e.target.value)} />
      </label>
      {hits.length > 0 && (
        <div className="search-results" role="listbox">
          {hits.map((h) => (
            <button key={h.qualified_name} onClick={() => choose(h.qualified_name)}>
              <span className="mono">{h.qualified_name}</span>{" "}
              <span className="muted">{h.kind.toLowerCase()}{h.is_test ? " · test" : ""} · {h.file_path}:{h.start_line}</span>
            </button>
          ))}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {focus && graph && (
        <div className="explorer" style={{ marginTop: 12 }}>
          <div>
            <CallGraph nodes={graph.nodes} edges={graph.edges} focus={focus} onSelect={choose} />
            <p className="muted">{graph.nodes.length} nodes · {graph.edges.length} edges · click a node to move the focus</p>
          </div>
          <div>
            {details && (
              <dl className="kv">
                <dt>Symbol</dt><dd className="mono">{details.qualified_name}</dd>
                <dt>Kind</dt><dd>{details.kind}{details.is_test ? " (test)" : ""}</dd>
                <dt>Defined in</dt><dd className="mono">{details.file_path}:{details.start_line}-{details.end_line}</dd>
                {details.signature && (<><dt>Signature</dt><dd className="mono">{details.signature}</dd></>)}
                {details.summary && (<><dt>LLM summary</dt><dd>{details.summary}</dd></>)}
                {details.unresolved_calls?.length > 0 && (
                  <><dt>Unresolved calls</dt><dd className="mono">{details.unresolved_calls.join(", ")}</dd></>)}
              </dl>
            )}
            {file && details && (
              <div style={{ marginTop: 12 }}>
                <CodeView key={`${file.path}:${details.start_line}`} code={file.content}
                          highlight={[details.start_line, details.end_line]} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
