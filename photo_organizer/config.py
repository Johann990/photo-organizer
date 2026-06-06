"""
config.py — Configuration file loader and validator.

Supports config.json with multiple input directories.

Example config.json
-------------------
{
    "db":                    "C:/photos.db",
    "target":                "E:/Organised",
    "workers":               4,
    "hamming_threshold":     8,
    "use_secondary_signals": false,
    "input_dirs": [
        "E:/Photos/2018-2022",
        "D:/Backup/Camera"
    ],
    "known_cameras": [
        { "make": "Sony",  "model": "ILCE-7RM2" },
        { "make": "Sony",  "model": "ILCE-7M4"  },
        { "make": "Apple", "model": "iPhone 14 Pro" }
    ]
}

Rules enforced
--------------
- input_dirs must be a non-empty list
- each input_dir must NOT be a filesystem root  (e.g. E:\\ or /)
- each input_dir must exist and be a directory
- no two input_dirs may be identical; overlapping paths produce a warning
- target (if given) must not sit inside an input_dir
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class PhotoConfig:
    db: Path
    input_dirs: list[Path]
    target: Path | None               = None
    workers: int                       = 4
    hamming_threshold: int             = 8
    use_secondary_signals: bool        = False
    known_cameras: list[dict[str, str]] = field(default_factory=list)

    # ── Loader ──────────────────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: Path) -> "PhotoConfig":
        """
        Load and parse config.json.
        Raises ValueError with a descriptive message on any problem.
        """
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"config.json is not valid JSON: {e}") from e

        if not isinstance(raw, dict):
            raise ValueError("config.json must be a JSON object { … }")

        # Required fields
        if "db" not in raw:
            raise ValueError("config.json missing required field: \"db\"")
        if "input_dirs" not in raw:
            raise ValueError("config.json missing required field: \"input_dirs\"")

        input_dirs_raw = raw["input_dirs"]
        if not isinstance(input_dirs_raw, list) or len(input_dirs_raw) == 0:
            raise ValueError(
                "\"input_dirs\" must be a non-empty list of directory paths"
            )

        return cls(
            db=Path(raw["db"]),
            input_dirs=[Path(p) for p in input_dirs_raw],
            target=Path(raw["target"]) if raw.get("target") else None,
            workers=int(raw.get("workers", 4)),
            hamming_threshold=int(raw.get("hamming_threshold", 8)),
            use_secondary_signals=bool(raw.get("use_secondary_signals", False)),
            known_cameras=raw.get("known_cameras", []),
        )

    # ── Validation ──────────────────────────────────────────────────────────

    def validate(self) -> tuple[list[str], list[str]]:
        """
        Return (errors, warnings).
        errors   — must be fixed before running
        warnings — should be reviewed but won't block execution
        """
        errors: list[str] = []
        warnings: list[str] = []

        # ── input_dirs ───────────────────────────────────────────────────────
        resolved: list[Path] = []

        for raw in self.input_dirs:
            p = raw.resolve() if raw.exists() else raw

            # Must not be a filesystem root
            if _is_root(raw):
                errors.append(
                    f"input_dir cannot be a root directory: {raw}\n"
                    f"  Specify a sub-directory, e.g. E:\\Photos\\2020"
                )
                continue

            # Must exist and be a directory
            if not raw.exists():
                errors.append(f"input_dir does not exist: {raw}")
                continue
            if not raw.is_dir():
                errors.append(f"input_dir is not a directory: {raw}")
                continue

            resolved.append(p)

        # Check for duplicates / overlapping paths
        for i, a in enumerate(resolved):
            for j, b in enumerate(resolved):
                if i >= j:
                    continue
                if a == b:
                    errors.append(f"Duplicate input_dir: {a}")
                elif b in a.parents:
                    warnings.append(
                        f"Overlapping input_dirs: {b} is a parent of {a}\n"
                        f"  Files in {a} will be scanned twice."
                    )
                elif a in b.parents:
                    warnings.append(
                        f"Overlapping input_dirs: {a} is a parent of {b}\n"
                        f"  Files in {b} will be scanned twice."
                    )

        # ── target ───────────────────────────────────────────────────────────
        if self.target is not None:
            if _is_root(self.target):
                errors.append(
                    f"target cannot be a root directory: {self.target}"
                )
            else:
                t = self.target.resolve() if self.target.exists() else self.target
                for src in resolved:
                    if t == src or src in t.parents:
                        errors.append(
                            f"target {self.target} is inside input_dir {src}.\n"
                            f"  This would cause organised files to be re-scanned."
                        )
                    elif t in src.parents:
                        warnings.append(
                            f"target {self.target} is a parent of input_dir {src}.\n"
                            f"  Organised files may overlap with source files."
                        )

        # ── db ───────────────────────────────────────────────────────────────
        db_parent = self.db.parent
        if not db_parent.exists():
            errors.append(
                f"DB parent directory does not exist: {db_parent}\n"
                f"  Create it first or choose a different path."
            )

        # ── workers ──────────────────────────────────────────────────────────
        if not (1 <= self.workers <= 64):
            warnings.append(f"workers={self.workers} is unusual (expected 1–64).")

        return errors, warnings

    def to_dict(self) -> dict[str, Any]:
        """Serialise back to a JSON-compatible dict."""
        return {
            "db": str(self.db),
            "target": str(self.target) if self.target else None,
            "workers": self.workers,
            "hamming_threshold": self.hamming_threshold,
            "use_secondary_signals": self.use_secondary_signals,
            "input_dirs": [str(p) for p in self.input_dirs],
            "known_cameras": self.known_cameras,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_root(p: Path) -> bool:
    """
    Return True if p is a filesystem root.
    Works on Windows (C:\\, D:\\) and POSIX (/).
    A path is root when its parent is itself.
    """
    try:
        return p.resolve().parent == p.resolve()
    except Exception:
        # Fallback for paths that don't exist yet
        return p == p.parent


def load_config(path: str | Path) -> PhotoConfig:
    """Convenience wrapper used by __main__.py."""
    return PhotoConfig.from_file(Path(path))
