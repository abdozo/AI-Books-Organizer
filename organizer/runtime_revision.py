from __future__ import annotations

import hashlib
from pathlib import Path


def application_revision(project_root: Path) -> str:
    """Return a short fingerprint for files that require a server restart."""
    root = Path(project_root)
    files = [
        root / "ai-books-organizer.html",
        root / "requirements.txt",
        root / "pyproject.toml",
        root / "run.py",
        root / "server_control.py",
        *sorted((root / "organizer").glob("*.py")),
        *sorted((root / "static").glob("*.js")),
    ]
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]
