"""Question families. Each family has

- a seed query: the targets it can ask about (deterministically ordered),
- a gold query: the exact answer, run per target and stored with the question (`gold_cypher`),
- a text template.

Gold queries only follow precise static bindings, never name-matched ones. Answers describe the
observed graph at a pinned commit; they do not promise complete runtime coverage.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.config import PRECISE_RESOLVERS
from app.graph.store import GraphStore

PRECISE = list(PRECISE_RESOLVERS)
MAX_ISSUE_CHARS = 1500


@dataclass
class Draft:
    family: str
    text: str
    gold_cypher: str
    gold_params: dict
    gold_answer: list[str]
    hop_count: int | None = None
    target: str | None = None
    meta: dict = field(default_factory=dict)


def display(qn: str, module: str) -> str:
    """Name relative to its module: `Session.send` for requests.sessions.Session.send."""
    return qn[len(module) + 1:] if module and qn.startswith(module + ".") else qn


class Family:
    name = ""

    def seeds(self, store: GraphStore, build_id: int) -> list[dict]:
        raise NotImplementedError

    def make(self, store: GraphStore, build_id: int, seed: dict, rng: random.Random) -> list[Draft]:
        raise NotImplementedError

    @staticmethod
    def _params(build_id: int, **extra) -> dict:
        return {"build_id": build_id, "precise": PRECISE, **extra}


class Lookup(Family):
    name = "lookup"
    SEED = """
MATCH (f:File {build_id: $build_id})-[:DEFINES]->(n)
WHERE n:Class OR (n:Function AND n.is_method = false)
WITH n.name AS name, collect(DISTINCT f) AS files, collect(DISTINCT labels(n)[0]) AS kinds
WHERE size(files) = 1 AND NOT files[0].is_test AND NOT name STARTS WITH '_'
RETURN name, files[0].path AS file, kinds[0] AS kind ORDER BY name"""
    GOLD = """
MATCH (f:File {build_id: $build_id})-[:DEFINES]->(n {name: $name})
WHERE n:Class OR (n:Function AND n.is_method = false)
RETURN DISTINCT f.path AS gold"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, {"build_id": build_id})

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, name=seed["name"])
        gold = [r["gold"] for r in store.read(self.GOLD, params)]
        kind = "class" if seed["kind"] == "Class" else "function"
        text = f"Which file defines the {kind} `{seed['name']}`? Answer with its repository-relative path."
        return [Draft(self.name, text, self.GOLD.strip(), params, gold, 1, seed["name"], {"kind": kind})]


class Files(Family):
    """Bounded, exact file inventory questions work even when a language has no symbol adapter."""

    name = "files"
    MAX_FILES = 10
    SEED = """
MATCH (f:File {build_id: $build_id})
WHERE f.language IS NOT NULL
RETURN DISTINCT f.language AS language ORDER BY language"""
    GOLD = """
MATCH (f:File {build_id: $build_id, language: $language})
RETURN f.path AS gold ORDER BY gold LIMIT $limit"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, self._params(build_id))

    def make(self, store, build_id, seed, rng):
        language = seed["language"]
        params = self._params(build_id, language=language, limit=self.MAX_FILES)
        gold = [r["gold"] for r in store.read(self.GOLD, params)]
        text = (f"According to this build's file inventory, list the first {self.MAX_FILES} repository-relative "
                f"paths indexed as `{language}` files, ordered alphabetically. "
                f"If fewer than {self.MAX_FILES} were indexed, list all of them.")
        return [Draft(self.name, text, self.GOLD.strip(), params, gold, None, language,
                      {"language": language, "limit": self.MAX_FILES, "coverage": "file_inventory"})]


class Callers(Family):
    name = "callers"
    SEED = """
MATCH (caller:Function {build_id: $build_id})-[r:CALLS]->(f:Function {build_id: $build_id})
WHERE r.resolved_by IN $precise AND NOT caller.is_test AND NOT f.in_test_file AND NOT f.name STARTS WITH '__'
WITH f, count(DISTINCT caller) AS n WHERE n <= 10
RETURN f.qualified_name AS qn, f.module AS module, f.file_path AS file ORDER BY qn"""
    GOLD = """
MATCH (caller:Function {build_id: $build_id})-[r:CALLS]->(:Function {build_id: $build_id, qualified_name: $qn})
WHERE r.resolved_by IN $precise AND NOT caller.is_test
RETURN DISTINCT caller.qualified_name AS gold"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, self._params(build_id))

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, qn=seed["qn"])
        gold = [r["gold"] for r in store.read(self.GOLD, params)]
        name = display(seed["qn"], seed["module"])
        text = (f"According to the precise call edges in the analyzed graph, which non-test functions "
                f"call `{name}` (defined in `{seed['file']}`) directly?")
        return [Draft(self.name, text, self.GOLD.strip(), params, gold, 1, seed["qn"])]


class Impact(Family):
    """Tests reaching a function within k call hops, for k = 1, 2, 3 (experiment E2)."""

    name = "impact"
    MAX_HOPS = 3
    SEED = """
MATCH (:Function {build_id: $build_id})-[r:CALLS]->(f:Function {build_id: $build_id})
WHERE r.resolved_by IN $precise AND NOT f.in_test_file AND NOT f.name STARTS WITH '__'
RETURN DISTINCT f.qualified_name AS qn, f.module AS module, f.file_path AS file ORDER BY qn"""
    GOLD = """
MATCH p = (:Function {build_id: $build_id, qualified_name: $qn})<-[:CALLS*1..%d]-(t:Function {is_test: true})
WHERE ALL(r IN relationships(p) WHERE r.resolved_by IN $precise)
RETURN t.qualified_name AS gold, min(length(p)) AS hops"""
    SUFFIX = {1: "", 2: " or through one intermediate function",
              3: " or through a chain of up to two intermediate functions"}

    def seeds(self, store, build_id):
        return store.read(self.SEED, self._params(build_id))

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, qn=seed["qn"])
        rows = store.read(self.GOLD % self.MAX_HOPS, params)
        name = display(seed["qn"], seed["module"])
        drafts = []
        for k in range(1, self.MAX_HOPS + 1):
            gold = sorted(r["gold"] for r in rows if r["hops"] <= k)
            needs_k = any(r["hops"] == k for r in rows)  # otherwise identical to the (k-1)-hop question
            if not gold or not needs_k:
                continue
            text = (f"According to the precise call edges in the analyzed graph, which test functions "
                    f"reach `{name}` (in `{seed['file']}`) within {k} call hops? "
                    f"Count a test if it calls `{name}` directly{self.SUFFIX[k]}. "
                    "This is a list of potential impact, not a prediction that those tests will fail.")
            drafts.append(Draft(self.name, text, (self.GOLD % k).strip(), params, gold, k, seed["qn"]))
        return drafts


class Inheritance(Family):
    name = "inheritance"
    SEED = """
MATCH p = (:Class {build_id: $build_id})-[:INHERITS*1..]->(b:Class {build_id: $build_id})
WHERE NOT b.in_test_file AND ALL(x IN relationships(p) WHERE x.resolved_by IN $precise)
RETURN DISTINCT b.qualified_name AS qn, b.module AS module, b.file_path AS file ORDER BY qn"""
    GOLD = """
MATCH p = (:Class {build_id: $build_id, qualified_name: $qn})<-[:INHERITS*1..]-(c:Class)
WHERE ALL(x IN relationships(p) WHERE x.resolved_by IN $precise)
RETURN c.qualified_name AS gold, min(length(p)) AS hops"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, self._params(build_id))

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, qn=seed["qn"])
        rows = store.read(self.GOLD, params)
        name = display(seed["qn"], seed["module"])
        text = (f"According to the precise inheritance edges in the analyzed graph, which classes "
                f"inherit from `{name}` (in `{seed['file']}`), directly or indirectly?")
        hops = max((r["hops"] for r in rows), default=None)
        return [Draft(self.name, text, self.GOLD.strip(), params, sorted(r["gold"] for r in rows), hops, seed["qn"])]


class History(Family):
    name = "history"
    SEED = """
MATCH (:Commit {build_id: $build_id})-[:MODIFIES]->(f:Function {build_id: $build_id})
WHERE NOT f.in_test_file AND NOT f.is_nested
RETURN DISTINCT f.qualified_name AS qn, f.module AS module, f.file_path AS file ORDER BY qn"""
    GOLD = """
MATCH (:Function {build_id: $build_id, qualified_name: $qn})<-[:MODIFIES]-(c:Commit)<-[:AUTHORED]-(a:Author)
OPTIONAL MATCH (pr:PullRequest)-[:INCLUDES]->(c)
WITH c, a, collect(DISTINCT pr.number) AS prs
RETURN c.sha AS sha, c.committed_at AS committed_at, a.name AS author, a.email AS email, a.login AS login,
       prs
ORDER BY c.committed_at DESC, c.order ASC LIMIT 2"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, {"build_id": build_id})

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, qn=seed["qn"])
        rows = store.read(self.GOLD, params)
        if not rows:
            return []
        last = rows[0]
        if len(rows) > 1 and rows[1]["committed_at"] == last["committed_at"] and rows[1]["sha"] != last["sha"]:
            return []  # two commits at the same second: "last" is ambiguous
        name = display(seed["qn"], seed["module"])
        aliases = [a for a in (last["email"], last["login"], (last["email"] or "").split("@")[0]) if a]
        gold = [last["author"]]
        prs = [n for n in last["prs"] or [] if n is not None]
        if len(prs) == 1:  # a commit in several PRs would make "which pull request" ambiguous: ask only who
            gold.append(f"#{prs[0]}")
            text = (f"In the recorded history, who last changed `{name}` (in `{seed['file']}`), and in which pull request? "
                    "Answer with the author's name and the PR number.")
        else:
            text = f"In the recorded history, who last changed `{name}` (in `{seed['file']}`)? Answer with the author's name."
        return [Draft(self.name, text, self.GOLD.strip(), params, gold, None, seed["qn"],
                      {"aliases": {last["author"]: aliases}, "sha": last["sha"]})]


class Localization(Family):
    name = "localization"
    SEED = """
MATCH (:PullRequest {build_id: $build_id})-[:FIXES]->(i:Issue {build_id: $build_id})
RETURN DISTINCT i.number AS issue, i.title AS title, i.body AS body ORDER BY issue"""
    GOLD = """
MATCH (:Issue {build_id: $build_id, number: $issue})<-[:FIXES]-(:PullRequest)-[:INCLUDES]->(:Commit)
      -[:MODIFIES]->(f:Function)
WHERE NOT f.in_test_file
RETURN DISTINCT f.qualified_name AS gold"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, {"build_id": build_id})

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, issue=seed["issue"])
        gold = sorted(r["gold"] for r in store.read(self.GOLD, params))
        return [Draft(self.name, issue_question(seed["issue"], seed["title"], seed["body"]), self.GOLD.strip(),
                      params, gold, None, f"#{seed['issue']}", {"issue": seed["issue"]})]


def issue_question(number: int | str, title: str, body: str | None) -> str:
    body = (body or "").strip()
    if len(body) > MAX_ISSUE_CHARS:
        body = body[:MAX_ISSUE_CHARS] + " [...]"
    return (f"Here is GitHub issue #{number}.\n\nTitle: {title}\n\n{body}\n\n"
            "Which functions in this repository need to change to fix this issue? "
            "List fully-qualified function names, most likely first.")


class FalsePremise(Family):
    """'Why does a call b?' for pairs in the same class/module. Two thirds have no call path at all
    (gold: empty list, the model should reject the premise); one third are true controls with a direct
    call (gold: the call chain [a, b]), so a model that always says "no" cannot score well."""

    name = "false_premise"
    CANDIDATES = """
MATCH (f:Function {build_id: $build_id})
WHERE NOT f.in_test_file AND NOT f.is_nested AND NOT f.name STARTS WITH '__' AND f.kind <> 'property'
RETURN f.qualified_name AS qn, f.name AS name, f.module AS module, f.class_qn AS class_qn,
       f.file_path AS file, f.code AS code, f.unresolved_calls AS unresolved ORDER BY qn"""
    GOLD = """
MATCH (a:Function {build_id: $build_id, qualified_name: $a}), (b:Function {build_id: $build_id, qualified_name: $b})
RETURN EXISTS { MATCH (a)-[:CALLS*1..3]->(b) } AS reaches,
       EXISTS { MATCH (a)-[r:CALLS]->(b) WHERE r.resolved_by IN $precise } AS direct_precise"""

    def seeds(self, store, build_id):
        rows = store.read(self.CANDIDATES, {"build_id": build_id})
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault((r["module"], r["class_qn"]), []).append(r)
        seeds = []
        for members in groups.values():
            if len(members) < 2:
                continue
            for i, a in enumerate(members):
                b = members[(i + 1) % len(members)]
                seeds.append({"a": a, "b": b, "kind": "false"})
        callers = store.read(
            "MATCH (a:Function {build_id: $build_id})-[r:CALLS]->(b:Function {build_id: $build_id}) "
            "WHERE r.resolved_by IN $precise AND NOT a.in_test_file AND NOT b.in_test_file AND a.module = b.module "
            "AND NOT b.name STARTS WITH '__' AND b.kind <> 'property' RETURN a.qualified_name AS a, b.qualified_name AS b, a.module AS module "
            "ORDER BY a, b", self._params(build_id))
        by_qn = {r["qn"]: r for r in rows}
        for c in callers:
            if c["a"] in by_qn and c["b"] in by_qn:
                seeds.append({"a": by_qn[c["a"]], "b": by_qn[c["b"]], "kind": "true"})
        return seeds

    def make(self, store, build_id, seed, rng):
        a, b = seed["a"], seed["b"]
        params = self._params(build_id, a=a["qn"], b=b["qn"])
        check = store.read(self.GOLD, params)[0]
        if seed["kind"] == "false":
            mentioned = b["name"] in (a["code"] or "") or b["name"] in (a["unresolved"] or [])
            if check["reaches"] or mentioned:
                return []
            gold: list[str] = []
        else:
            if not check["direct_precise"]:
                return []
            gold = [a["qn"], b["qn"]]
        an, bn = display(a["qn"], a["module"]), display(b["qn"], b["module"])
        text = (f"In the analyzed graph, why does `{an}` call `{bn}` (both in `{a['file']}`)? "
                f"Answer with the observed call chain from `{an}` to `{bn}` as fully-qualified names. "
                "Return an empty list if no such chain is recorded; graph absence does not prove runtime absence.")
        return [Draft(self.name, text, self.GOLD.strip(), params, gold, None, a["qn"],
                      {"premise": seed["kind"] == "true", "b": b["qn"]})]


class Codegen(Family):
    """E4: blank out a tested function and ask for it back. Gold = the repo functions the original calls;
    the symbol checker grades hallucinated APIs, and the sandbox can run the tests in meta.tests."""

    name = "codegen"
    SEED = """
MATCH (f:Function {build_id: $build_id})-[r:CALLS]->(g:Function)
WHERE NOT f.in_test_file AND NOT f.is_nested AND r.resolved_by IN $precise AND f.kind <> 'property'
  AND coalesce(f.language, 'python') = 'python' AND f.file_path ENDS WITH '.py'
  AND f.end_line - f.def_line >= 2 AND f.end_line - f.def_line <= 40 AND NOT f.name STARTS WITH '__'
WITH f, collect(DISTINCT g.qualified_name) AS callees WHERE size(callees) <= 10
RETURN f.qualified_name AS qn, f.module AS module, f.file_path AS file, f.signature AS signature,
       f.docstring AS docstring, f.class_qn AS class_qn, f.start_line AS start_line, f.end_line AS end_line,
       f.def_line AS def_line, f.is_async AS is_async, f.decorators AS decorators
ORDER BY qn"""
    GOLD = """
MATCH (:Function {build_id: $build_id, qualified_name: $qn})-[r:CALLS]->(g:Function)
WHERE r.resolved_by IN $precise
RETURN DISTINCT g.qualified_name AS gold"""
    TESTS = """
MATCH p = (:Function {build_id: $build_id, qualified_name: $qn})<-[:CALLS*1..3]-(t:Function {is_test: true})
WHERE ALL(r IN relationships(p) WHERE r.resolved_by IN $precise)
RETURN DISTINCT t.pytest_id AS id ORDER BY id LIMIT 25"""

    def seeds(self, store, build_id):
        return store.read(self.SEED, self._params(build_id))

    def make(self, store, build_id, seed, rng):
        params = self._params(build_id, qn=seed["qn"])
        gold = sorted(r["gold"] for r in store.read(self.GOLD, params))
        tests = [r["id"] for r in store.read(self.TESTS, params) if r["id"]]
        name = display(seed["qn"], seed["module"])
        lines = [f"Implement `{name}` in `{seed['file']}`.", "",
                 f"Signature: `{'async ' if seed['is_async'] else ''}def {seed['signature']}`"]
        if seed["class_qn"]:
            lines.append(f"It is a method of `{display(seed['class_qn'], seed['module'])}`.")
        if seed.get("decorators"):  # kept from the original file; they decide what the first parameter is
            lines.append("Decorators: " + " ".join(f"`@{d}`" for d in seed["decorators"]))
        if seed["docstring"]:
            lines += ["Docstring:", '"""', seed["docstring"], '"""']
        lines.append("Write the complete function.")
        meta = {"language": "python", "module": seed["module"], "file": seed["file"], "class_qn": seed["class_qn"],
                "signature": seed["signature"], "start_line": seed["start_line"], "def_line": seed["def_line"],
                "end_line": seed["end_line"],
                "tests": tests, "blank": seed["qn"]}
        return [Draft(self.name, "\n".join(lines), self.GOLD.strip(), params, gold, None, seed["qn"], meta)]


FAMILIES: dict[str, Family] = {f.name: f for f in (Lookup(), Files(), Callers(), Impact(), Inheritance(), History(),
                                                   Localization(), FalsePremise(), Codegen())}
QA_FAMILIES = [n for n in FAMILIES if n != "codegen"]
