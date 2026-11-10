#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_bat_encoding.py  --  SAS project batch-file encoding normalizer.

Problem it solves
------------------
On Chinese Windows, cmd.exe parses .bat/.cmd files using the system ANSI
codepage (GBK) by default. A UTF-8 *without BOM* file that contains Chinese
bytes gets mangled: comments like `REM 中文` break into bogus commands
("... is not recognized as an internal or external command") and cd paths
get corrupted. The only signal cmd.exe honors to read a batch file as UTF-8
from byte 0 is a UTF-8 BOM.

Fix
---
For every *.bat / *.cmd / *.ps1 under the project root (excluding
node_modules / .venv / .git), normalize to:
    - UTF-8 WITH BOM  (so cmd reads it as UTF-8; harmless for pure-ASCII too)
    - CRLF line endings (required for `if (...) (...)` blocks in batch)
Idempotent and safe: pure-ASCII files just gain a BOM (no behavior change).

If --git-add is passed, only files already staged for this commit are
re-staged after normalization (so the hook never pollutes the commit with
unrelated changes).

Usage
-----
    python fix_bat_encoding.py                 # normalize whole project
    python fix_bat_encoding.py --check         # report only, no writes
    python fix_bat_encoding.py --git-add       # normalize + re-stage changed
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Project root = parents of this file: backend/scripts -> backend -> root
ROOT = Path(__file__).resolve().parents[2]
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
PATTERNS = ("*.bat", "*.cmd", "*.ps1")
BOM = b"\xef\xbb\xbf"


def decode_bytes(data: bytes) -> str:
    """Decode as UTF-8 first, then GBK, then lossy UTF-8 as last resort."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def normalize_file(path: Path) -> bool:
    """Return True if the file was changed on disk."""
    raw = path.read_bytes()
    if raw[:3] == BOM:
        raw = raw[3:]
    text = decode_bytes(raw)
    # Normalize line endings to CRLF
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    if text and not text.endswith("\r\n"):
        text += "\r\n"
    out = BOM + text.encode("utf-8")
    if out != path.read_bytes():
        path.write_bytes(out)
        return True
    return False


def collect_files(root: Path):
    files = []
    for pat in PATTERNS:
        for p in root.rglob(pat):
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            files.append(p)
    return sorted(files, key=lambda x: str(x))


def git_staged(root: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=False,
        )
        return set(out.stdout.splitlines())
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT), help="project root to scan")
    ap.add_argument("--check", action="store_true", help="report only, no writes")
    ap.add_argument("--git-add", action="store_true",
                    help="re-stage only already-staged changed files")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    files = collect_files(root)
    staged = git_staged(root) if args.git_add else set()

    changed = []
    chinese_fixed = 0
    for p in files:
        rel = p.relative_to(root).as_posix()
        before = p.read_bytes()
        had_cjk = any(0x4E00 <= b <= 0x9FFF for b in before)
        if args.check:
            if before[:3] != BOM or b"\r\n" not in before:
                changed.append((rel, "needs BOM/CRLF" + (" (has Chinese)" if had_cjk else "")))
            continue
        if normalize_file(p):
            changed.append((rel, "normalized" + (" (had Chinese)" if had_cjk else "")))
            if had_cjk:
                chinese_fixed += 1

    if args.git_add and changed:
        to_add = [c[0] for c in changed if c[0] in staged]
        if to_add:
            subprocess.run(["git", "-C", str(root), "add", "--", *to_add],
                           check=False)

    print(f"Scanned {len(files)} .bat/.cmd/.ps1 files under {root}")
    if changed:
        print(f"Changed {len(changed)} file(s):")
        for rel, msg in changed:
            print(f"  [OK] {rel}  -- {msg}")
        if chinese_fixed:
            print(f"  -> {chinese_fixed} file(s) with Chinese repaired (now UTF-8 BOM).")
    else:
        print("All files already UTF-8-BOM + CRLF. Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
