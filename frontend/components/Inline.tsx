/** Text with `backticked` spans rendered as inline code. */
export function Inline({ text }: { text: string }) {
  return <>{text.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : <span key={i}>{part}</span>))}</>;
}

export const VERDICT_ICON: Record<string, string> = { supported: "✓", contradicted: "✕", unverifiable: "?" };
