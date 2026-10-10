"""Map the many ways a model can write a name onto the graph's canonical names.

`Session.send`, `requests.sessions.Session.send()`, `src/requests/sessions.py::Session::send` and
`def send` all mean the same function when the suffix is unique. Matching rules, in order:
exact qualified name -> unique dotted suffix in the repo -> unique suffix among the gold items
(lenient: the model named something the question already pinned down) -> left as written.
"""

from __future__ import annotations

import re

from app.eval.index import BuildIndex

_WRAP = " \t\n`'\"*"
_PR = re.compile(r"#\s*(\d+)")
_PR_WORDS = re.compile(r"\b(pull request|pr|in|by|via|and|merged|commit)\b", re.IGNORECASE)


def clean_item(item: object) -> str:
    s = str(item).strip().strip(_WRAP)
    s = re.sub(r"^(?:async\s+def|def|class)\s+", "", s)
    s = re.sub(r"\(.*\)$", "", s)  # foo() / foo(a, b)
    return s.rstrip(":").strip().strip(_WRAP)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def normalize_symbol(item: object, index: BuildIndex, gold: set[str]) -> str:
    s = clean_item(item)
    path, sep, rest = s.partition("::")
    if path.endswith(".py"):  # pytest node id or "file.py::Class::method"
        module = index.module_for_path(path)
        if module:
            s = module + ("." + rest.replace("::", ".") if sep and rest else "")
    elif sep:
        s = s.replace("::", ".")
    if index.is_entity(s):
        return s
    candidates = index.symbols_ending_with(s)
    if len(candidates) == 1:
        return next(iter(candidates))
    in_gold = candidates & gold
    if len(in_gold) == 1:
        return next(iter(in_gold))
    return s


def normalize_path(item: object, index: BuildIndex, gold: set[str]) -> str:
    s = clean_item(item).replace("\\", "/").removeprefix("./").lstrip("/")
    if s in index.files:
        return s
    if s in index.modules:  # a dotted module name instead of a path
        return index.modules[s]
    candidates = index.paths_ending_with(s)
    if len(candidates) == 1:
        return next(iter(candidates))
    in_gold = candidates & gold
    return next(iter(in_gold)) if len(in_gold) == 1 else s


def normalize_history(items: list[object], aliases: dict[str, list[str]]) -> list[str]:
    """Split answers like "Kenneth Reitz in PR #1234" into an author and "#1234"; map author aliases."""
    lookup = {}
    for canonical, alts in aliases.items():
        for a in [canonical, *alts]:
            lookup[a.lower()] = canonical
    out: list[str] = []
    for item in items:
        s = clean_item(item)
        prs = _PR.findall(s)
        if not prs and s.isdigit():
            prs = [s]
            s = ""
        out.extend(f"#{n}" for n in prs)
        name = " ".join(_PR_WORDS.sub(" ", _PR.sub(" ", s)).split()).strip(" ,;-()")
        if name:
            out.append(lookup.get(name.lower(), name))
    return _dedupe(out)


def normalize_items(family: str, items: list[object], index: BuildIndex, gold: list[str],
                    meta: dict | None = None) -> list[str]:
    gold_set = set(gold)
    if family == "lookup":
        return _dedupe([normalize_path(i, index, gold_set) for i in items])
    if family == "history":
        return normalize_history(items, (meta or {}).get("aliases", {}))
    return _dedupe([normalize_symbol(i, index, gold_set) for i in items])
