const ICON: Record<string, string> = { done: "✓", failed: "✕", running: "◌", queued: "…" };

export default function StatusBadge({ status, progress }: { status: string; progress?: string | null }) {
  return (
    <span className={`badge ${status}`} title={progress ?? undefined}>
      <span className="dot" aria-hidden />
      <span aria-hidden>{ICON[status] ?? ""}</span>
      {status}
      {status === "running" && progress ? <span className="muted"> · {progress}</span> : null}
    </span>
  );
}
