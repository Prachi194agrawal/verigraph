export type JobStatus = "queued" | "running" | "done" | "failed";

export interface Repo {
  id: number;
  url: string;
  name: string;
  commit_sha: string | null;
  created_at: string;
}

export interface Build {
  id: number;
  graph_id: number;
  repo_id: number;
  repo: string;
  commit_sha: string | null;
  parser_version: string;
  status: JobStatus;
  progress: string | null;
  options: Record<string, unknown>;
  stats: Record<string, any> | null;
  unresolved_call_rate: number | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Dataset {
  id: number;
  build_id: number;
  version: string;
  status: JobStatus;
  progress: string | null;
  options: Record<string, unknown>;
  n_questions: number;
  error: string | null;
  created_at: string;
}

export interface Question {
  id: number;
  family: string;
  text: string;
  template_text: string;
  gold_cypher: string;
  gold_answer: string[];
  hop_count: number | null;
  target: string | null;
  meta: Record<string, any>;
}

export interface Experiment {
  id: number;
  dataset_id: number;
  model: string;
  pipeline: string;
  prompt_version: string;
  judge_model: string | null;
  status: JobStatus;
  progress: string | null;
  summary: Summary | null;
  error: string | null;
  created_at: string;
}

export interface Summary {
  n: number;
  errors: number;
  f1: number | null;
  f1_ci: [number | null, number | null];
  f1_by_family: Record<string, number | null>;
  n_by_family: Record<string, number>;
  f1_by_hop: Record<string, number | null>; // impact questions only (E2)
  f1_by_inheritance_depth?: Record<string, number | null>;
  cost_per_question: number | null;
  latency_ms_mean: number | null;
  latency_ms_median: number | null;
  tokens_per_question: number | null;
  context_recall: number | null;
  claim_precision: number | null;
  contradicted_claim_rate: number | null;
  invented_item_rate: number | null;
  faithfulness: number | null;
  hit_at_1: number | null;
  hit_at_5: number | null;
  false_premise_rejection_rate: number | null;
  codegen?: Record<string, number | null>;
}

export interface LeaderRow extends Summary {
  experiment_id: number;
  model: string;
  pipeline: string;
  status: JobStatus;
  prompt_version: string;
}

export interface HopPoint {
  model: string;
  pipeline: string;
  hop: number;
  f1: number | null;
}

export interface AnswerRow {
  answer_id: number;
  question_id: number;
  family: string;
  hop_count: number | null;
  latency_ms: number;
  cost_usd: number;
  tokens: number;
  error: string | null;
  scores: Record<string, number>;
}

export interface GraphNode {
  id: string;
  kind: "Function" | "Class";
  name: string;
  file_path: string;
  start_line: number;
  end_line: number;
  is_test: boolean;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: "CALLS" | "INHERITS" | "TESTS" | "DEFINES";
  resolved_by: string | null;
  line: number | null;
}

export interface Claim {
  id: number;
  subject: string;
  relation: string;
  object: string;
  verdict: "supported" | "contradicted" | "unverifiable";
  evidence_edge: string | null;
  extractor: string;
  claim_text: string | null;
}

export interface SymbolUse {
  symbol: string;
  kind: string;
  exists: boolean;
  arity_ok: boolean | null;
  category: string | null;
  detail: string | null;
  line: number | null;
}

export interface AnswerChecks {
  build_id: number | null;
  answer: {
    id: number;
    text: string;
    parsed_items: string[];
    match: { raw?: string[]; tp?: string[]; fp?: string[]; fn?: string[]; invented?: string[] };
    explanation: string | null;
    code: string | null;
    retrieved_nodes: string[];
    steps: { cypher: string; rows: number; error: string | null }[];
    latency_ms: number;
    tokens: number;
    cost_usd: number;
    error: string | null;
    trace_id: string | null;
  };
  question: {
    id: number;
    family: string;
    text: string;
    gold_answer: string[];
    gold_cypher: string;
    hop_count: number | null;
    meta: Record<string, any>;
    build_params: Record<string, any>;
  };
  experiment: { id: number; model: string; pipeline: string; dataset_id: number };
  claims: Claim[];
  symbols: SymbolUse[];
  test_runs: { tests_run: number; passed: number; failed: number; status: string; log_path: string | null }[];
  scores: Record<string, number>;
  labels: { id: number; label: string; claim_verdict_id: number | null; notes: string | null }[];
}

export const FAMILIES = [
  "lookup",
  "callers",
  "impact",
  "inheritance",
  "history",
  "localization",
  "false_premise",
  "codegen",
] as const;

export const PIPELINES = ["none", "vector", "graphrag", "cypher_agent"] as const;

// Color follows the pipeline (never its rank): the same slot on every chart.
export const PIPELINE_SLOT: Record<string, number> = { none: 1, vector: 2, graphrag: 3, cypher_agent: 4 };
export const PIPELINE_LABEL: Record<string, string> = {
  none: "No retrieval",
  vector: "Vector RAG",
  graphrag: "GraphRAG",
  cypher_agent: "Cypher agent",
};

export interface AskResult {
  question: string;
  model: string;
  pipeline: string;
  answer: { raw: string; name: string; in_graph: boolean | null; kind: string | null }[];
  explanation: string;
  raw_text: string;
  error: string | null;
  claims: { subject: string; relation: string; object: string; verdict: Claim["verdict"]; evidence: string;
            extractor: string; claim_text: string }[];
  summary: { claims: number; supported: number; contradicted: number; unverifiable: number; invented_items: number };
  context: string;
  retrieved_nodes: string[];
  steps: { cypher: string; rows: number; error: string | null }[];
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  trace_id: string;
}

export interface ImpactRow {
  qualified_name: string;
  file_path: string;
  hops: number;
}

export interface ImpactResult {
  max_hops: number;
  changed: { qualified_name: string; file_path: string }[];
  unmatched: string[];
  affected: ImpactRow[];
  tests: (ImpactRow & { pytest_id: string | null })[];
  possibly_affected: ImpactRow[];
  markdown: string;
}

export interface SymbolAudit {
  functions_checked: number;
  functions_flagged: number;
  false_positive_rate: number | null;
  by_category: Record<string, number>;
  examples: { function: string; symbol: string; category: string; detail: string; line: number | null }[];
}
