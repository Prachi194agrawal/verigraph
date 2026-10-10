"""Claim-by-claim checking of free-text explanations against the graph (RefChecker-style triplets).

1. Extract (subject, relation, object) claims: regex rules always, plus an LLM extractor when a judge
   model is configured.
2. Resolve subject/object to graph entities and label each claim:
   - supported     a precise edge/path proves it
   - contradicted  both entities exist, the relation is covered by the graph, and no edge exists
   - unverifiable  unknown entity, only a name-matched edge, or the subject has an unresolved call that
                   could be the claimed one (the graph cannot rule it out)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import PRECISE_RESOLVERS
from app.eval.index import BuildIndex
from app.eval.normalize import clean_item, normalize_path, normalize_symbol
from app.llm.client import LLMClient
from app.llm.prompts import render

RELATIONS = ("calls", "not_calls", "inherits", "defined_in", "tests", "modified_by")

_SYM = r"`[^`\n]+`|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+(?:\(\))?|[A-Za-z_]\w*\(\)"
_SYMS = rf"(?:{_SYM})(?:\s*(?:,|,?\s+and|,?\s+or)\s+(?:{_SYM}))*"
_PATH = r"`?[\w./-]+\.py`?"
_RULES: list[tuple[str, re.Pattern]] = [
    ("not_calls", re.compile(rf"(?P<a>{_SYM})\s+(?:does\s+not|doesn't|never|do\s+not|cannot)\s+(?:directly\s+)?"
                             rf"(?:call|invoke|use)s?\s+(?P<b>{_SYMS})", re.IGNORECASE)),
    ("calls", re.compile(rf"(?P<a>{_SYM})\s+(?:\w+ly\s+|also\s+|then\s+|first\s+|later\s+)?(?:calls|invokes)\s+"
                         rf"(?P<b>{_SYMS})", re.IGNORECASE)),
    ("called_by", re.compile(rf"(?P<a>{_SYMS})\s+(?:is|are)\s+(?:\w+ly\s+)?(?:called|invoked)\s+(?:by|from)\s+"
                             rf"(?P<b>{_SYMS})", re.IGNORECASE)),
    ("inherits", re.compile(rf"(?P<a>{_SYM})\s+(?:\w+ly\s+)?(?:inherits\s+from|extends|subclasses|derives\s+from|"
                            rf"is\s+a\s+subclass\s+of)\s+(?P<b>{_SYMS})", re.IGNORECASE)),
    ("defined_in", re.compile(rf"(?P<a>{_SYM})\s+is\s+(?:defined|declared|implemented)\s+in\s+(?P<b>{_PATH})",
                              re.IGNORECASE)),
    ("tests", re.compile(rf"(?P<a>{_SYM})\s+(?:tests|exercises)\s+(?P<b>{_SYMS})", re.IGNORECASE)),
    ("modified_by", re.compile(rf"(?P<a>{_SYM})\s+was\s+(?:last\s+|most\s+recently\s+)?(?:changed|modified|updated|"
                               rf"edited)\s+by\s+(?P<b>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){{0,3}})")),
]
_SPLIT = re.compile(r"\s*(?:,\s*and|,\s*or|,|\band\b|\bor\b)\s*")


@dataclass
class Claim:
    subject: str
    relation: str
    object: str
    text: str = ""
    extractor: str = "rule"


@dataclass
class Verdict:
    claim: Claim
    label: str  # supported | contradicted | unverifiable
    evidence: str


def _split_names(text: str) -> list[str]:
    names = re.findall(r"`([^`\n]+)`", text)
    if names:
        return names
    return [n for n in _SPLIT.split(text) if n]


def extract_claims_rules(text: str) -> list[Claim]:
    claims: list[Claim] = []
    for sentence in re.split(r"(?<=[.;!?])\s+|\n+", text or ""):
        for relation, pattern in _RULES:
            for m in pattern.finditer(sentence):
                subjects = _split_names(m.group("a")) if relation == "called_by" else [m.group("a").strip("`")]
                objects = [m.group("b").strip("`")] if relation in ("defined_in", "modified_by") \
                    else _split_names(m.group("b"))
                subjects = [x.strip().rstrip(".,;:") for x in subjects]
                objects = [x.strip().rstrip(".,;:") for x in objects]
                for s in subjects:
                    for o in objects:
                        if relation == "called_by":  # "b is called by a" -> (a, calls, b)
                            claims.append(Claim(o, "calls", s, sentence.strip()))
                        else:
                            claims.append(Claim(s, relation, o, sentence.strip()))
    unique = {(c.subject, c.relation, c.object): c for c in claims}
    return list(unique.values())


def extract_claims_llm(text: str, llm: LLMClient, model: str) -> list[Claim]:
    data, _ = llm.complete_json(model, [{"role": "user", "content": render("claim_extract", text=text)}],
                                max_tokens=800, name="claim_extract")
    out = []
    for c in (data or {}).get("claims", []) if isinstance(data, dict) else []:
        if isinstance(c, dict) and c.get("relation") in RELATIONS and c.get("subject") and c.get("object"):
            out.append(Claim(str(c["subject"]), c["relation"], str(c["object"]), extractor="llm"))
    return out


def _fn(index: BuildIndex, name: str) -> str | None:
    qn = normalize_symbol(name, index, set())
    return qn if qn in index.functions else None


def _cls(index: BuildIndex, name: str) -> str | None:
    qn = normalize_symbol(name, index, set())
    return qn if qn in index.classes else None


def check_claim(claim: Claim, index: BuildIndex) -> Verdict:
    rel = claim.relation

    def verdict(label: str, evidence: str) -> Verdict:
        return Verdict(claim, label, evidence)

    if rel in ("calls", "not_calls"):
        a, b = _fn(index, claim.subject), _fn(index, claim.object)
        if a is None or b is None:
            target = b or (index.classes and _cls(index, claim.object))
            if a and target and target in index.classes:  # "f calls Session" = instantiation
                b = index.constructor(target)
            if a is None or b is None:
                return verdict("unverifiable", "subject or object is not a function in the graph")
        by = index.calls.get((a, b))
        precise = by in PRECISE_RESOLVERS
        possible = index.functions[b].name in index.functions[a].unresolved_calls
        if rel == "calls":
            if precise:
                return verdict("supported", f"({a})-[:CALLS {{line: {index.call_lines.get((a, b))}, resolved_by: {by}}}]->({b})")
            if by == "name":
                return verdict("unverifiable", f"only a name-matched CALLS edge {a} -> {b}")
            if possible:
                return verdict("unverifiable", f"{a} has an unresolved call named {index.functions[b].name}")
            unresolved = index.functions[a].unresolved_calls
            note = f"; {a} also has unresolved calls: {', '.join(unresolved[:5])}" if unresolved else ""
            return verdict("contradicted", f"no CALLS edge from {a} to {b}{note}")
        if precise:
            return verdict("contradicted", f"({a})-[:CALLS {{resolved_by: {by}}}]->({b}) exists")
        if by == "name" or possible:
            return verdict("unverifiable", f"the graph cannot rule out {a} -> {b}")
        return verdict("supported", f"no CALLS edge from {a} to {b}")

    if rel == "inherits":
        a, b = _cls(index, claim.subject), _cls(index, claim.object)
        if a is None or b is None:
            return verdict("unverifiable", "subject or object is not a class in the graph")
        found = index.inherits_from(a, b)
        if found == "precise":
            return verdict("supported", f"({a})-[:INHERITS*]->({b})")
        if found == "name":
            return verdict("unverifiable", f"only name-matched INHERITS edges connect {a} to {b}")
        return verdict("contradicted", f"no INHERITS path from {a} to {b}")

    if rel == "defined_in":
        qn = normalize_symbol(claim.subject, index, set())
        actual = index.file_of(qn)
        if actual is None:
            return verdict("unverifiable", "subject is not in the graph")
        claimed = normalize_path(claim.object, index, {actual})
        if claimed == actual:
            return verdict("supported", f"(:File {{path: {actual}}})-[:DEFINES]->({qn})")
        return verdict("contradicted", f"{qn} is defined in {actual}")

    if rel == "tests":
        t, f = _fn(index, claim.subject), _fn(index, claim.object)
        if t is None or f is None or not index.functions[t].is_test:
            return verdict("unverifiable", "subject is not a test function in the graph")
        hops = index.reaches(t, f, 3)
        if hops:
            return verdict("supported", f"({t})-[:CALLS*{hops}]->({f})")
        return verdict("contradicted", f"no precise CALLS path of <= 3 hops from {t} to {f}")

    if rel == "modified_by":
        f = _fn(index, claim.subject)
        if f is None or f not in index.modified_by:
            return verdict("unverifiable", "no history for this function in the graph")
        who = clean_item(claim.object).lower()
        if who in index.modified_by[f]:
            return verdict("supported", f"(:Author {{name: {claim.object}}})-[:AUTHORED]->(:Commit)-[:MODIFIES]->({f})")
        return verdict("contradicted", f"{f} was modified by {sorted(index.modified_by[f])[:5]}")

    return verdict("unverifiable", f"relation {rel!r} is not covered by the graph")


def check_explanation(text: str, index: BuildIndex, llm: LLMClient | None = None,
                      model: str | None = None) -> list[Verdict]:
    claims = extract_claims_rules(text)
    if llm is not None and model:
        seen = {(c.subject.strip("`"), c.relation, c.object.strip("`")) for c in claims}
        for c in extract_claims_llm(text, llm, model):
            if (c.subject.strip("`"), c.relation, c.object.strip("`")) not in seen:
                claims.append(c)
    return [check_claim(c, index) for c in claims]
