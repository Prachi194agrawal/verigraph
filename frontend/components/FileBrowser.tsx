"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const CodeView = dynamic(() => import("./CodeView"), { ssr: false });

interface SourceFile { path: string; language: string; analysis_level: string; loc: number }

export default function FileBrowser({ buildId }: { buildId: number }) {
  const [query, setQuery] = useState("");
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [selected, setSelected] = useState<SourceFile | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      api<SourceFile[]>(`/builds/${buildId}/files?q=${encodeURIComponent(query)}&limit=50`)
        .then((next) => { if (!cancelled) { setFiles(next); setError(null); } })
        .catch((err) => { if (!cancelled) setError(String(err)); });
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [buildId, query]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setContent(null);
    setError(null);
    api<{ content: string }>(`/builds/${buildId}/file?path=${encodeURIComponent(selected.path)}`)
      .then((next) => { if (!cancelled) setContent(next.content); })
      .catch((err) => { if (!cancelled) setError(String(err)); });
    return () => { cancelled = true; };
  }, [buildId, selected]);

  return <section className="card" id="files">
    <h2>Browse source files by language</h2>
    <p className="secondary">Find a file even when you do not know a function name. Inventory-only languages have readable source and file-level questions; function calls and change-impact analysis need structural support.</p>
    <div className="explorer">
      <div>
        <label className="field">Filter by file path
          <input type="text" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. src/, main, or .ts" />
        </label>
        <div className="search-results" style={{ marginTop: 10 }} aria-label="Source files">
          {files.map((file) => <button type="button" key={file.path} onClick={() => setSelected(file)} aria-pressed={selected?.path === file.path}>
            <span className="mono">{file.path}</span><br />
            <span className="secondary">{file.language} · {file.analysis_level === "file" ? "file inventory" : "symbols and static calls"} · {file.loc} lines</span>
          </button>)}
        </div>
        {files.length === 50 && <p className="secondary">Showing the first 50 matches. Filter by path to find more files.</p>}
        {files.length === 0 && !error && <p className="secondary">No indexed files match this filter.</p>}
      </div>
      <div>
        {selected ? <>
          <h3 className="mono" style={{ marginTop: 0 }}>{selected.path}</h3>
          {content !== null ? <CodeView key={selected.path} code={content} path={selected.path} height={360} />
            : !error && <p className="secondary">Loading source…</p>}
        </> : <p className="secondary">Select a file to read its source at this build&apos;s commit.</p>}
      </div>
    </div>
    {error && <p className="error">{error}</p>}
  </section>;
}
