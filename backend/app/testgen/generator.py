"""Build a dataset: sample targets per family, compute gold with Cypher, filter, optionally rephrase."""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable

from app.graph.store import GraphStore
from app.llm.client import LLMClient
from app.testgen.filters import keep
from app.testgen.rephraser import SKIP_FAMILIES, rephrase
from app.testgen.templates import FAMILIES, Draft, Impact


def generate_questions(store: GraphStore, build_id: int, *, families: list[str], per_family: int, seed: int = 0,
                       llm: LLMClient | None = None, rephrase_model: str | None = None,
                       progress: Callable[[str], None] | None = None) -> list[Draft]:
    unknown = set(families) - FAMILIES.keys()
    if unknown:
        raise ValueError(f"unknown families {sorted(unknown)}; choose from {sorted(FAMILIES)}")
    rng = random.Random(seed)
    seen: set[tuple[str, str]] = set()
    out: list[Draft] = []
    for name in families:
        family = FAMILIES[name]
        if progress:
            progress(f"generating {name} questions")
        seeds = family.seeds(store, build_id)  # deterministic order, then a seeded shuffle
        rng.shuffle(seeds)
        drafts: list[Draft] = []
        per_hop: Counter[int] = Counter()
        hop_cap = math.ceil(per_family / Impact.MAX_HOPS)
        premise: Counter[bool] = Counter()
        for s in seeds:
            if len(drafts) >= per_family:
                break
            for d in family.make(store, build_id, s, rng):
                if len(drafts) >= per_family or not keep(d, seen):
                    continue
                if name == "impact":  # balance hop depths for E2
                    if per_hop[d.hop_count] >= hop_cap:
                        continue
                    per_hop[d.hop_count] += 1
                if name == "false_premise":  # two false premises for every true control
                    is_true = bool(d.meta.get("premise"))
                    if premise[is_true] >= (per_family // 3 if is_true else per_family - per_family // 3):
                        continue
                    premise[is_true] += 1
                drafts.append(d)
        for d in drafts:
            d.meta["reference_source"] = "static_code_graph"
            d.meta.setdefault("coverage", "observed_precise_edges" if name in {
                "callers", "impact", "inheritance", "false_premise", "codegen"} else "recorded_graph_facts")
        out.extend(drafts)

    if rephrase_model and llm is not None:
        for i, d in enumerate(out):
            if progress and i % 10 == 0:
                progress(f"rephrasing questions {i}/{len(out)}")
            if d.family not in SKIP_FAMILIES:
                d.meta["template_text"] = d.text
                d.text = rephrase(llm, rephrase_model, d.text)
    return out
