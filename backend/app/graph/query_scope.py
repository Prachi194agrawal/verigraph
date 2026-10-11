"""Restrict model-written Cypher to simple read queries over one build.

This is deliberately a small subset, not a general Cypher parser. Unsupported
constructs fail closed and the agent receives the error so it can simplify its query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Token:
    text: str
    start: int
    end: int


_TOKENS = re.compile(
    r"\s+|//[^\n]*|/\*[\s\S]*?\*/|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|"
    r"`(?:[^`]|``)*`|\$[A-Za-z_]\w*|[A-Za-z_]\w*|\d+(?:\.\d+)?|[^\s]"
)
_FORBIDDEN = {
    "CALL", "UNION", "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "LOAD",
    "FOREACH", "GRANT", "DENY", "REVOKE", "SHOW", "USE", "START", "INSERT", "FINISH",
}
_END_MATCH = {"WHERE", "RETURN", "WITH", "UNWIND", "ORDER", "SKIP", "LIMIT", "OPTIONAL", "MATCH"}


def scope_query(cypher: str) -> str:
    tokens = [Token(m.group(), m.start(), m.end()) for m in _TOKENS.finditer(cypher)
              if not m.group().isspace() and not m.group().startswith(("//", "/*"))]
    if not tokens or tokens[0].text.upper() not in {"MATCH", "OPTIONAL"}:
        raise ValueError("Start with MATCH; only simple graph read queries are supported")
    words = [t.text.upper() for t in tokens]
    if any(w in _FORBIDDEN for w in words) or ";" in words:
        raise ValueError("Procedures, subqueries, multiple statements and write clauses are not supported")
    # Namespaced functions can execute a query supplied as a string (for example
    # apoc.cypher.runFirstColumn), bypassing the MATCH patterns we scope below.
    if any(t.text == "." and i + 2 < len(tokens) and tokens[i + 2].text == "("
           for i, t in enumerate(tokens)):
        raise ValueError("Namespaced functions are not supported; use built-in read functions")
    edits: list[tuple[int, int, str]] = []
    in_match = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        upper = t.text.upper()
        if upper == "MATCH":
            in_match = True
            i += 1
            continue
        if in_match and upper in _END_MATCH:
            in_match = False
        if not in_match:
            # Reject graph-pattern expressions and subqueries outside MATCH.
            nxt = tokens[i + 1].text if i + 1 < len(tokens) else ""
            if t.text == "{" or (t.text in {"-", "<", "]"} and nxt in {"-", "[", ">"}):
                raise ValueError("Use MATCH for graph traversal; subqueries and pattern expressions are unsupported")
            i += 1
            continue
        if t.text == "[":
            # Relationship patterns can contain only types and length bounds, not
            # predicates/functions that might traverse another graph.
            end = next((j for j in range(i + 1, len(tokens)) if tokens[j].text == "]"), None)
            if end is None or any(x.text in {"(", "{", "["} for x in tokens[i + 1:end]):
                raise ValueError("Unsupported relationship pattern")
            i = end + 1
            continue
        if t.text == "(":
            end = next((j for j in range(i + 1, len(tokens)) if tokens[j].text == ")"), None)
            if end is None or any(x.text == "(" for x in tokens[i + 1:end]):
                raise ValueError("Unsupported node pattern")
            inner = tokens[i + 1:end]
            brace = next((j for j, x in enumerate(inner) if x.text == "{"), None)
            head = inner if brace is None else inner[:brace]
            head_text = "".join(x.text for x in head)
            if not re.fullmatch(r"(?:[A-Za-z_]\w*|`(?:[^`]|``)+`)?(?::(?:[A-Za-z_]\w*|`(?:[^`]|``)+`))*", head_text):
                raise ValueError("Only variable/label node patterns are supported")
            if brace is None:
                edits.append((tokens[end].start, tokens[end].start, " {build_id: $build_id}"))
            else:
                body = inner[brace:]
                if body[-1].text != "}" or any(x.text in {"{", "}"} for x in body[1:-1]):
                    raise ValueError("Unsupported node property map")
                key = next((j for j, x in enumerate(body) if x.text.strip("`") == "build_id"
                            and j + 1 < len(body) and body[j + 1].text == ":"), None)
                if key is not None:
                    if key + 2 >= len(body) or body[key + 2].text != "$build_id" or (
                            key + 3 < len(body) and body[key + 3].text not in {",", "}"}):
                        raise ValueError("build_id must use the supplied $build_id parameter")
                else:
                    comma = ", " if len(body) > 2 else ""
                    edits.append((body[-1].start, body[-1].start, comma + "build_id: $build_id"))
            i = end + 1
            continue
        if t.text in {"{", "}", ")", "|"}:
            raise ValueError("Unsupported MATCH pattern")
        i += 1
    if not edits and not any(t.text == "$build_id" for t in tokens):
        raise ValueError("Query has no scoped graph pattern")
    for start, end, replacement in reversed(edits):
        cypher = cypher[:start] + replacement + cypher[end:]
    return cypher
