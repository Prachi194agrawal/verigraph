"use client";

import Editor, { loader, type OnMount } from "@monaco-editor/react";
import { useEffect, useState } from "react";

// Monaco is served by this app (copied to public/ by scripts/copy-monaco.mjs), not fetched from a CDN.
// Language workers run in their own scope and need an absolute same-origin URL.
loader.config({ paths: { vs: typeof window === "undefined" ? "/monaco/vs" : new URL("/monaco/vs", window.location.origin).href } });

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
export function codeLanguage(path?: string | null): string {
  const extension = path?.split(".").pop()?.toLowerCase();
  const languages: Record<string, string> = { py: "python", pyi: "python", js: "javascript", jsx: "javascript",
    mjs: "javascript", cjs: "javascript", ts: "typescript", mts: "typescript", cts: "typescript", tsx: "typescript", java: "java", go: "go", rs: "rust",
    c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", hxx: "cpp", cs: "csharp", rb: "ruby", rake: "ruby", php: "php",
    kt: "kotlin", kts: "kotlin", swift: "swift", scala: "scala", sc: "scala", sh: "shell", bash: "shell", lua: "lua",
    r: "r", dart: "dart", ex: "elixir", exs: "elixir", clj: "clojure", cljs: "clojure", cljc: "clojure", jl: "julia",
    pl: "perl", pm: "perl", m: "objective-c", mm: "objective-c", html: "html", htm: "html", css: "css", scss: "scss",
    less: "less", sql: "sql", proto: "protobuf", ps1: "powershell", psm1: "powershell", json: "json", jsonc: "json",
    yaml: "yaml", yml: "yaml", toml: "plaintext", md: "markdown", mdx: "markdown" };
  if (path?.split("/").pop() === "Dockerfile") return "dockerfile";
  return extension ? languages[extension] ?? "plaintext" : "python";
}

export default function CodeView({ code, highlight, markers, height = 460, path, language }: {
  code: string;
  path?: string | null;
  language?: string | null;
  highlight?: [number, number] | null;
  markers?: CodeMarker[];
  height?: number;
}) {
  const dark = usePrefersDark();
  const onMount: OnMount = (editor, monaco) => {
    // This is a source viewer, not a TypeScript project with its dependencies loaded.
    monaco.languages.typescript?.typescriptDefaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: true });
    monaco.languages.typescript?.javascriptDefaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: true });
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
      language={language || codeLanguage(path)}
      value={code}
      theme={dark ? "vs-dark" : "light"}
      onMount={onMount}
      options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, scrollBeyondLastLine: false,
                 renderLineHighlight: "none", wordWrap: "on" }}
    />
  );
}
