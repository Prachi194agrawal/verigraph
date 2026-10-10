"use client";

import Editor, { loader, type OnMount } from "@monaco-editor/react";
import { useEffect, useState } from "react";

// Monaco is served by this app (copied to public/ by scripts/copy-monaco.mjs), not fetched from a CDN.
loader.config({ paths: { vs: "/monaco/vs" } });

export interface CodeMarker {
  line: number;
  message: string;
  severity: "error" | "warning";
}

function usePrefersDark(): boolean {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const explicit = document.documentElement.dataset.theme;
    if (explicit) {
      setDark(explicit === "dark");
      return;
    }
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    setDark(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return dark;
}

/** Read-only Monaco view. `highlight` marks a line range (a function); `markers` flag lines (missing symbols). */
export default function CodeView({ code, highlight, markers, height = 460 }: {
  code: string;
  highlight?: [number, number] | null;
  markers?: CodeMarker[];
  height?: number;
}) {
  const dark = usePrefersDark();
  const onMount: OnMount = (editor, monaco) => {
    if (highlight) {
      editor.revealLineInCenter(highlight[0]);
      editor.createDecorationsCollection([{
        range: new monaco.Range(highlight[0], 1, highlight[1], 1),
        options: { isWholeLine: true, className: "line-highlight" },
      }]);
    }
    const model = editor.getModel();
    if (model && markers?.length) {
      monaco.editor.setModelMarkers(model, "verigraph", markers.map((m) => ({
        startLineNumber: m.line, endLineNumber: m.line, startColumn: 1,
        endColumn: model.getLineMaxColumn(Math.min(m.line, model.getLineCount())),
        message: m.message,
        severity: m.severity === "error" ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
      })));
    }
  };
  return (
    <Editor
      height={height}
      language="python"
      value={code}
      theme={dark ? "vs-dark" : "light"}
      onMount={onMount}
      options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false,
                 renderLineHighlight: "none", wordWrap: "on" }}
    />
  );
}
