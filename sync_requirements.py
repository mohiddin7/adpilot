#!/usr/bin/env python3
"""
sync_requirements.py
=====================
Scans every .py file in this project, works out which imports are
third-party packages, and syncs requirements.txt against what's
*actually installed* in the current environment.

What it does:
  - import found with no matching entry in requirements.txt -> appended
  - existing entry whose pin doesn't match what's installed  -> rewritten
    to the installed version (extras like [standard] are preserved)
  - entry in requirements.txt that no .py file imports        -> left
    alone, but reported so you can decide whether to drop it
  - import that can't be matched to an installed package      -> reported,
    not written (pip install it, then re-run)

Local/first-party modules (lib/, pipelines/, pages/, etc.) and the
standard library are ignored automatically - it never tries to add
your own code to requirements.txt.

Requires Python >= 3.10 (uses sys.stdlib_module_names and
importlib.metadata.packages_distributions()).

Usage (run with the project's venv ACTIVE, so "installed" reflects the
right environment):
    source .venv/bin/activate
    python sync_requirements.py            # scan + write requirements.txt
    python sync_requirements.py --dry-run  # scan + report only, no write
"""

import argparse
import ast
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
SELF_PATH = Path(__file__).resolve()

EXCLUDED_DIR_NAMES = {
    ".venv", "venv", "env", "__pycache__", ".git",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist", ".eggs",
    "node_modules",
}

# google.* is a namespace package - many distributions register under the
# top-level name "google", so a plain name -> distribution lookup doesn't
# work. Map specific dotted paths to the distribution that actually
# provides them. Extend this if the project starts importing other
# google.cloud.* clients (storage, pubsub, secret manager, etc.).
NAMESPACE_HINTS = {
    "google.cloud.bigquery": "google-cloud-bigquery",
    "google.cloud.storage": "google-cloud-storage",
    "google.cloud": "google-cloud-core",
    "google.oauth2": "google-auth",
    "google.auth": "google-auth",
    "google.api_core": "google-api-core",
}
NAMESPACE_PREFIXES = ("google",)

# Matches simple "name", "name==1.2.3", "name[extra]>=1.2.3" lines.
# Anything with environment markers (";...") or other syntax this doesn't
# handle is left untouched rather than risk mangling it.
REQ_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"\s*(?P<spec>==|>=|<=|~=|!=|>|<)?"
    r"\s*(?P<version>[A-Za-z0-9.*+!_-]*)\s*$"
)


def normalize(name: str) -> str:
    """PEP 503 normalization, for comparing package names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def find_py_files(root: Path):
    for path in root.rglob("*.py"):
        if path.resolve() == SELF_PATH:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
            continue
        yield path


def extract_candidates(py_file: Path) -> set[str]:
    """Return the set of top-level import 'candidates' for one file."""
    try:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"  ! could not parse {py_file.relative_to(PROJECT_ROOT)}: {exc}")
        return set()

    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in NAMESPACE_PREFIXES:
                    candidates.add(alias.name)  # full dotted path
                else:
                    candidates.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import -> always local
            if not node.module:
                continue
            top = node.module.split(".")[0]
            if top in NAMESPACE_PREFIXES:
                # "from google.cloud import bigquery" -> "google.cloud.bigquery"
                for alias in node.names:
                    candidates.add(f"{node.module}.{alias.name}")
            else:
                candidates.add(top)
    return candidates


def build_local_names(py_files) -> set[str]:
    """Filenames and containing-directory names -> treated as first-party."""
    names: set[str] = set()
    for f in py_files:
        names.add(f.stem)
        names.add(f.parent.name)
    return names


def get_version(dist_name: str):
    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def resolve_candidate(candidate: str, local_names: set, dist_map: dict):
    """
    Classify one import candidate.
    Returns (status, payload):
      "resolved"   -> payload = (dist_name, version)
      "stdlib"     -> payload = None
      "local"      -> payload = None
      "ambiguous"  -> payload = (candidate, [dist_names...])
      "unresolved" -> payload = candidate
    """
    top = candidate.split(".")[0]

    if top in NAMESPACE_PREFIXES:
        parts = candidate.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in NAMESPACE_HINTS:
                version = get_version(NAMESPACE_HINTS[prefix])
                if version:
                    return "resolved", (NAMESPACE_HINTS[prefix], version)
        dists = dist_map.get(top)
        if dists:
            if len(dists) > 1:
                return "ambiguous", (candidate, dists)
            version = get_version(dists[0])
            if version:
                return "resolved", (dists[0], version)
        return "unresolved", candidate

    if top in sys.stdlib_module_names:
        return "stdlib", None

    dists = dist_map.get(top)
    if dists:
        if len(dists) > 1:
            return "ambiguous", (candidate, dists)
        version = get_version(dists[0])
        if version:
            return "resolved", (dists[0], version)

    if top in local_names:
        return "local", None

    return "unresolved", candidate


def load_requirements(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def sync_requirements(lines: list[str], resolved: dict):
    """
    lines: existing requirements.txt lines (or [] if file doesn't exist)
    resolved: {normalized_dist_name: (dist_name, version)}
    Returns (new_lines, summary)
    """
    handled: set[str] = set()
    new_lines: list[str] = []
    updated: list[tuple] = []
    unreferenced: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            new_lines.append(line)
            continue

        m = REQ_LINE_RE.match(stripped)
        if not m:
            # marker/extra syntax this doesn't rewrite - leave untouched
            new_lines.append(line)
            continue

        name = m.group("name")
        extras = m.group("extras") or ""
        key = normalize(name)

        if key in resolved:
            dist_name, version = resolved[key]
            new_spec = f"{dist_name}{extras}=={version}"
            if stripped != new_spec:
                updated.append((stripped, new_spec))
            new_lines.append(new_spec)
            handled.add(key)
        else:
            unreferenced.append(stripped)
            new_lines.append(line)

    added: list[str] = []
    for key, (dist_name, version) in sorted(resolved.items(), key=lambda kv: kv[1][0].lower()):
        if key not in handled:
            new_lines.append(f"{dist_name}=={version}")
            added.append(f"{dist_name}=={version}")

    return new_lines, {"added": added, "updated": updated, "unreferenced": unreferenced}


def main():
    parser = argparse.ArgumentParser(description="Sync requirements.txt with actual imports + installed versions.")
    parser.add_argument("--dry-run", action="store_true", help="report only, don't write requirements.txt")
    args = parser.parse_args()

    py_files = list(find_py_files(PROJECT_ROOT))
    local_names = build_local_names(py_files)
    dist_map = metadata.packages_distributions()

    all_candidates: set[str] = set()
    for f in py_files:
        all_candidates |= extract_candidates(f)

    resolved: dict = {}
    ambiguous: list = []
    unresolved: list = []

    for c in sorted(all_candidates):
        status, payload = resolve_candidate(c, local_names, dist_map)
        if status == "resolved":
            dist_name, version = payload
            resolved[normalize(dist_name)] = (dist_name, version)
        elif status == "ambiguous":
            ambiguous.append(payload)
        elif status == "unresolved":
            unresolved.append(payload)
        # "stdlib" and "local" need no action

    existing_lines = load_requirements(REQUIREMENTS_FILE)
    new_lines, summary = sync_requirements(existing_lines, resolved)

    print(f"Scanned {len(py_files)} .py files under {PROJECT_ROOT}")
    print(
        f"Summary: {len(summary['added'])} to add, "
        f"{len(summary['updated'])} to update, "
        f"{len(summary['unreferenced'])} unreferenced, "
        f"{len(ambiguous)} ambiguous, {len(unresolved)} unresolved\n"
    )

    if summary["added"]:
        print("New packages found in imports, will be added:")
        for line in summary["added"]:
            print(f"  + {line}")
        print()

    if summary["updated"]:
        print("Existing pins that don't match what's installed, will be updated:")
        for old, new in summary["updated"]:
            print(f"  - {old}")
            print(f"  + {new}")
        print()

    if summary["unreferenced"]:
        print("In requirements.txt but no import found for them (left as-is, review manually):")
        for line in summary["unreferenced"]:
            print(f"  ? {line}")
        print()

    if ambiguous:
        print("Imports where multiple installed distributions match (resolve manually):")
        for candidate, dists in ambiguous:
            print(f"  ? {candidate} -> one of {dists}")
        print()

    if unresolved:
        print("Imports that don't match anything installed (pip install + re-run, or check for typos):")
        for c in unresolved:
            print(f"  ? {c}")
        print()

    if args.dry_run:
        print("Dry run - requirements.txt not written.")
        return

    if not summary["added"] and not summary["updated"]:
        print("requirements.txt already up to date.")
        return

    REQUIREMENTS_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"Wrote {REQUIREMENTS_FILE.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()