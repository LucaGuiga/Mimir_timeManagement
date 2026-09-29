#!/usr/bin/env python3
"""Validates staged filenames against the Mimir naming schema. Called by the pre-commit hook with the staged paths as argv.
Reads the Mimir config path from .mimir in the course repo root. Exit 1 on errors, 0 otherwise (warnings are printed)."""
import os
import subprocess
import sys


def repo_root():
    try:
        return subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return os.getcwd()


def main(argv):
    root = repo_root()
    marker = os.path.join(root, ".mimir")
    if not os.path.exists(marker):
        print("mimir: no .mimir file in the repo root, skipping naming check")
        return 0
    with open(marker, encoding="utf-8") as f:
        cfg_path = f.readline().strip()
    mimir_root = os.path.dirname(os.path.dirname(os.path.abspath(cfg_path)))
    sys.path.insert(0, mimir_root)
    try:
        from core.config import load_config
        from core.naming_schema import validate_filename
        cfg = load_config(cfg_path)
    except Exception as e:
        print(f"mimir: cannot load Mimir from {mimir_root} ({e}), skipping naming check")
        return 0
    folders = {f.lower() for f in (cfg.get("repo_manager", {}) or {}).get("default_folders", []) or []}
    try:
        existing = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=root).stdout.split()
    except Exception:
        existing = []
    siblings = [os.path.basename(p) for p in list(argv) + existing]
    errors, warnings = [], []
    for path in argv:
        parts = path.replace("\\", "/").split("/")
        base = parts[-1]
        if base.startswith(".") or len(parts) < 2 or parts[0].lower() not in folders:
            continue
        r = validate_filename(base, cfg, siblings=[s for s in siblings if s != base])
        errors += [f"  {path}: {e.split(': ', 1)[-1]}" for e in r["errors"]]
        warnings += [f"  {path}: {w.split(': ', 1)[-1]}" for w in r["warnings"]]
    if warnings:
        print("mimir naming warnings:")
        print("\n".join(warnings))
    if errors:
        print("mimir naming errors (commit blocked):")
        print("\n".join(errors))
        print("Rename the files to {Type}{number}_q{question}_part{part}_{COURSECODE} and stage them again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
