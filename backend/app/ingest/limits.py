"""Reject oversized checkouts before parsing, embeddings or graph writes."""

import os
from pathlib import Path

from app.config import Settings
from app.graph.languages import ADDITIONAL_SKIP_DIRS
from app.graph.modules import SKIP_DIRS


def check_checkout_limits(root: Path, settings: Settings) -> None:
    count = size = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS | ADDITIONAL_SKIP_DIRS
                   and not d.startswith(".") and not (Path(directory) / d).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            count += 1
            size += path.stat().st_size
            if count > settings.max_repo_files or size > settings.max_repo_bytes:
                raise ValueError("Repository exceeds MAX_REPO_FILES or MAX_REPO_BYTES; use a smaller repository")
