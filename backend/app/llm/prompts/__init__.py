"""Versioned prompt templates. Placeholders use string.Template syntax ($name); write $$ for a literal $.

Changing a prompt means adding a new version file (e.g. answer_system.v2.txt) and running experiments
with prompt_version="v2", so old results stay comparable. The CI eval gate runs on changes to this folder.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPT_DIR = Path(__file__).parent


@lru_cache
def load_prompt(name: str, version: str = "v1") -> Template:
    path = PROMPT_DIR / f"{name}.{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"prompt {name!r} has no version {version!r} ({path.name})")
    return Template(path.read_text(encoding="utf-8"))


def render(name: str, version: str = "v1", **values) -> str:
    return load_prompt(name, version).substitute(**values).strip()
