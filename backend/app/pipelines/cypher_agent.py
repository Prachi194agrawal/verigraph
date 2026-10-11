"""Cypher-writing agent (CodexGraph-style): the model writes a read-only query, sees the rows, and repeats
until it answers. Plain JSON turns instead of provider tool-calling, so it works with any model.

Code-writing tasks are not supported: the agent could simply read the original function from the graph.
"""

from __future__ import annotations

import json
import time

from app.graph.store import is_read_only
from app.llm.client import extract_json
from app.llm.prompts import render
from app.pipelines.base import Pipeline, PipelineOutput, QuestionInput
from app.pipelines.parsing import _flatten

SCHEMA = """Graph schema (node labels with key properties, then relationships):
(:Repo {name, url, commit_sha})-[:CONTAINS]->(:Module {name, path, is_package})-[:CONTAINS]->(:File {path, module, language, analysis_level, is_test, loc})
(:File)-[:DEFINES]->(:Class {qualified_name, name, module, file_path, bases, external_bases, start_line, end_line, docstring, is_test})
(:File)-[:DEFINES]->(:Function {qualified_name, name, module, language, file_path, class_qn, signature, param_names, kind, start_line, end_line, docstring, summary, is_test, is_method, is_nested, pytest_id})
(:Class)-[:DEFINES]->(:Function)                 methods
(:Function)-[:DEFINES]->(:Function)              nested functions
(:Function)-[:CALLS {line, lines, resolved_by}]->(:Function)
(:Class)-[:INHERITS {resolved_by}]->(:Class)
(:Function {is_test: true})-[:TESTS]->(:Function)
(:File)-[:IMPORTS {line}]->(:Module) and (:File)-[:IMPORTS]->(:ExternalPackage {name, is_stdlib})
(:Author {name, email, login})-[:AUTHORED]->(:Commit {sha, short_sha, title, message, committed_at})
(:Commit)-[:MODIFIES {lines_added, lines_deleted}]->(:Function)
(:PullRequest {number, title, body, author, merged_at})-[:INCLUDES]->(:Commit)
(:PullRequest)-[:FIXES]->(:Issue {number, title, body, state})
(:Function)-[:IMPLEMENTS {confidence}]->(:Feature {name, description})

Qualified names are canonical graph identifiers; Python names look like "package.module.Class.method".
Other languages include their language and file namespace. Use the stored identifiers verbatim.
Calls are static observations; missing relationships do not prove runtime absence.
committed_at is an ISO-8601 string.
Example, the callers of a method:
MATCH (c:Function {build_id: $build_id})-[:CALLS]->(f:Function {build_id: $build_id})
WHERE f.qualified_name ENDS WITH '.Session.send' RETURN c.qualified_name"""


def _format_rows(rows: list[dict], max_rows: int) -> str:
    shown = []
    for row in rows[:max_rows]:
        clean = {k: (v[:300] + "..." if isinstance(v, str) and len(v) > 300 else v) for k, v in row.items()}
        shown.append(json.dumps(clean, default=str))
    more = f"\n... {len(rows) - max_rows} more rows" if len(rows) > max_rows else ""
    return "\n".join(shown) + more if shown else "(no rows)"


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return [f"#{value}"] if isinstance(value, int) else []


class CypherAgentPipeline(Pipeline):
    name = "cypher_agent"

    def answer(self, q: QuestionInput, model: str) -> PipelineOutput:
        ctx, tracer = self.ctx, self.ctx.llm.tracer
        out = PipelineOutput(trace_id=tracer.new_trace_id())
        if q.family == "codegen":
            out.error = "cypher_agent does not run code-writing tasks (it could read the original function)"
            return out
        max_queries = int(ctx.options.get("agent_max_queries", 4))
        max_rows = int(ctx.options.get("agent_max_rows", 30))
        system = render("cypher_agent", ctx.prompt_version, repo=ctx.repo_name,
                        commit=(ctx.commit_sha or "working tree")[:12], schema=SCHEMA, max_rows=max_rows,
                        max_queries=max_queries)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": f"Question: {q.text}"}]
        started = time.perf_counter()
        context_parts: list[str] = []
        queries = 0
        try:
            for turn in range(max_queries + 3):
                resp = ctx.llm.complete(model, messages, json_mode=True, max_tokens=700, trace_id=out.trace_id,
                                        name=f"cypher_agent:turn{turn}")
                out.prompt_tokens += resp.prompt_tokens
                out.completion_tokens += resp.completion_tokens
                out.cost_usd += resp.cost_usd
                out.latency_ms += resp.latency_ms
                messages.append({"role": "assistant", "content": resp.text})
                data = extract_json(resp.text)
                action = data.get("action") if isinstance(data, dict) else None
                if action == "query" and queries < max_queries:
                    queries += 1
                    cypher = str(data.get("cypher", ""))
                    step = {"cypher": cypher, "rows": 0, "error": None}
                    if not is_read_only(cypher):
                        result = "Error: only read-only queries are allowed."
                        step["error"] = "write clause rejected"
                    else:
                        q_started = time.perf_counter()
                        try:
                            rows = ctx.store.read_scoped(cypher, ctx.build_id, timeout=15)
                            step["rows"] = len(rows)
                            result = _format_rows(rows, max_rows)
                            out.retrieved_nodes += _strings(rows[:max_rows])
                        except Exception as exc:  # noqa: BLE001 - errors are fed back to the model
                            result = f"Error: {exc}"[:600]
                            step["error"] = result
                        out.latency_ms += (time.perf_counter() - q_started) * 1000
                    out.steps.append(step)
                    context_parts.append(f"Query: {cypher}\nResult:\n{result}")
                    tracer.log(out.trace_id, "cypher", {"input": cypher, "output": result})
                    messages.append({"role": "user", "content": f"Result ({step['rows']} rows):\n{result}"})
                    continue
                if action == "answer" or (isinstance(data, dict) and "answer" in data):
                    out.raw_text = resp.text
                    out.items = _flatten(data.get("answer"))
                    out.explanation = str(data.get("explanation") or "")
                    break
                note = ("You have used all your queries. Reply now with the answer JSON object."
                        if queries >= max_queries else "Reply with exactly one JSON object, as instructed.")
                messages.append({"role": "user", "content": note})
            else:  # the model's failure, scored as an empty answer; `error` is kept for failed calls, which are retried
                out.raw_text = resp.text
                out.explanation = "The agent did not produce an answer within its turn limit."
        except Exception as exc:  # noqa: BLE001
            out.error = f"{type(exc).__name__}: {exc}"[:2000]
        out.context = "\n\n".join(context_parts)
        out.retrieved_nodes = list(dict.fromkeys(out.retrieved_nodes))
        if not out.latency_ms:
            out.latency_ms = (time.perf_counter() - started) * 1000
        return out
