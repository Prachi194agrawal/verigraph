"use client";

import type { Capabilities } from "@/lib/types";
import { usePoll } from "@/lib/usePoll";

export function useCapabilities() {
  return usePoll<Capabilities>("/capabilities", 30000);
}

export function ModelStatus({ model, capabilities }: { model?: string; capabilities: Capabilities | null }) {
  const providers = capabilities?.providers.filter((p) => p.name !== "mock") ?? [];
  const configured = providers.filter((p) => p.configured);
  return (
    <div className="notice">
      {model?.split(",").some((m) => m.trim().startsWith("mock/")) && (
        <p><strong>Demo model selected.</strong> Mock models test the workflow. Their answers and scores do not measure real AI understanding.</p>
      )}
      {capabilities ? (
        <p><strong>AI setup:</strong>{" "}
          {configured.length ? <>Configured providers: {configured.map((p) => p.name).join(", ")}.</>
            : <>No real model provider is configured. Explore the graph and change impact without an AI key, or use <code>mock/extractive</code> to try the evaluation workflow.</>}
          {" "}For a real model, enter an exact model name available to your provider account. Credentials belong in the server&apos;s <code>.env</code> file; restart the API and worker after changing them. Configured credentials do not confirm a model is available.
        </p>
      ) : <p>Enter the model name configured on your server. <code>mock/extractive</code> is available for a demo without an API key.</p>}
    </div>
  );
}

export function EmbeddingNote({ model }: { model?: string | null }) {
  if (!model) return null;
  return <p className="secondary">Code search uses <code>{model}</code>.{model === "hash"
    ? " Hash embeddings compare words and names. Use neural embeddings to evaluate semantic similarity retrieval."
    : " Embeddings retrieve similar code; graph relationships provide structural evidence."}</p>;
}

export function LanguageCoverage({ capabilities }: { capabilities: Capabilities | null }) {
  if (!capabilities) return <p className="secondary">Supported language coverage will appear when the API connects.</p>;
  return <details className="coverage">
    <summary>Supported languages and analysis limits ({capabilities.languages.length})</summary>
    <p>Repositories may contain several languages. Analysis follows supported source files; dynamic calls and connections between languages may be incomplete.</p>
    <div className="table-wrap"><table>
      <thead><tr><th>Language</th><th>File types</th><th>Analysis level</th></tr></thead>
      <tbody>{capabilities.languages.map((language) => <tr key={language.name}>
        <td>{language.name}</td><td className="mono">{language.extensions.join(", ")}</td><td>{language.level.replaceAll("_", " ")}</td>
      </tr>)}</tbody>
    </table></div>
    {capabilities.notes?.map((note) => <p className="secondary" key={note}>{note}</p>)}
  </details>;
}
