#!/usr/bin/env python3
"""memkit — deterministic, zero-API checks for file-based layered memory.

A memory workspace is plain Markdown organised in layers:

* L0   raw session transcripts (never touched by this tool)
* L0.5 summarised session logs
* L1   one fact / decision / lesson per Markdown file
* L2   per-domain context files distilled from L1, with provenance
* L3   a small router/index file loaded at session start

A human-authored JSON *spec* declares memory roots, L2 contexts with their
L1 sources, typed relationships, and symlink wiring.  ``refresh``
materialises SHA-256 hashes into a generated *manifest*.  ``check`` proves
the manifest still matches the files on disk and audits links, orphans,
duplicates, and relationship integrity.  ``link`` wires live directories to
their canonical location via symlinks with dry-run, timestamped backups,
and idempotent re-runs.

Design constraints (enforced by the test-suite):

* Python standard library only.
* No network access, no embedding model, no LLM, no daemon.
* Generated output is deterministic and atomically written.
* Automation never overwrites human-approved statements: the manifest is
  the only file this tool writes (plus symlinks for ``link``), and it is
  regenerated from the spec, never merged by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCHEMA_VERSION = 1
ALLOWED_RELATION_TYPES = {"derived_from", "supersedes", "contradicts", "depends_on"}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRONTMATTER_NAME_RE = re.compile(r"(?m)^name:\s*(.+?)\s*$")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def normalized_bytes(path: Path) -> bytes:
    """Read a file with line endings normalised, so hashes survive CRLF."""
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(normalized_bytes(path)).hexdigest()


def sha256_json(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def workspace_path(workspace: Path, raw: str) -> Path:
    """Resolve a workspace-relative path and refuse escapes."""
    path = (workspace / raw).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {raw}") from exc
    return path


def relative(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def memory_files(root: Path, archive_dir: str) -> List[Path]:
    files = list(root.glob("*.md"))
    archive = root / archive_dir
    if archive.is_dir():
        files.extend(archive.glob("*.md"))
    return sorted(path for path in files if path.is_file())


def memory_slug(value: str) -> str:
    """Treat ``feedback-a-b`` and ``feedback_a_b.md`` as one logical ID."""
    stem = value[:-3] if value.lower().endswith(".md") else value
    return re.sub(r"[-_]+", "-", stem.casefold())


def all_named_memories(
    workspace: Path, roots: List[Dict[str, Any]]
) -> Dict[str, List[Path]]:
    """Map every basename and declared frontmatter ``name:`` to its files."""
    named: Dict[str, List[Path]] = defaultdict(list)
    for item in roots:
        root = workspace_path(workspace, item["path"])
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            keys = {path.name}
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.startswith("---\n"):
                closing = text.find("\n---", 4)
                frontmatter = text[:closing] if closing >= 0 else text[:4096]
                match = FRONTMATTER_NAME_RE.search(frontmatter)
                if match:
                    declared = match.group(1).strip().strip("\"'")
                    if declared:
                        keys.add(declared if declared.endswith(".md") else f"{declared}.md")
            for key in keys:
                named[key].append(path)
    return named


def all_slugged_memories(named: Dict[str, List[Path]]) -> Dict[str, List[Path]]:
    slugged: Dict[str, List[Path]] = defaultdict(list)
    for paths in named.values():
        for path in paths:
            slugged[memory_slug(path.name)].append(path)
    return slugged


# --------------------------------------------------------------------------
# Spec handling
# --------------------------------------------------------------------------

def load_spec(spec_path: Path) -> Dict[str, Any]:
    spec = read_json(spec_path)
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"spec schema_version must be {SCHEMA_VERSION}")
    roots = spec.get("memory_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("memory_roots must be a non-empty list")
    return spec


def archive_dir_name(spec: Dict[str, Any]) -> str:
    return str(spec.get("archive_dir", "_archive"))


def missing_link_exceptions(spec: Dict[str, Any]) -> Tuple[Set[str], List[str]]:
    """Exact, human-explained historical exceptions ("<file>-><target>")."""
    allowed: Set[str] = set()
    issues: List[str] = []
    for index, item in enumerate(spec.get("known_missing_link_exceptions", [])):
        if not isinstance(item, dict):
            issues.append(f"invalid missing-link exception #{index}: expected object")
            continue
        reference = item.get("reference")
        reason = item.get("reason")
        if not isinstance(reference, str) or "->" not in reference:
            issues.append(f"invalid missing-link exception #{index}: exact reference required")
            continue
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            issues.append(f"invalid missing-link exception #{index}: reason required")
            continue
        allowed.add(reference)
    return allowed, issues


# --------------------------------------------------------------------------
# Manifest generation (the automated part; humans never edit its output)
# --------------------------------------------------------------------------

def generate_manifest(workspace: Path, spec: Dict[str, Any]) -> Dict[str, Any]:
    contexts: List[Dict[str, Any]] = []
    generated_relations: List[Dict[str, Any]] = []
    for context in spec.get("contexts", []):
        context_path = workspace_path(workspace, context["path"])
        if not context_path.is_file():
            raise ValueError(f"context file missing: {context['path']}")
        sources: List[Dict[str, str]] = []
        for source in context.get("sources", []):
            source_path = workspace_path(workspace, source["path"])
            if not source_path.is_file():
                raise ValueError(
                    f"context source missing for {context['path']}: {source['path']}"
                )
            record = {
                "path": relative(workspace, source_path),
                "role": source.get("role", "evidence"),
                "sha256": sha256_path(source_path),
            }
            sources.append(record)
            generated_relations.append(
                {
                    "from": relative(workspace, context_path),
                    "type": "derived_from",
                    "to": record["path"],
                    "scope": record["role"],
                    "origin": "generated_from_context_sources",
                }
            )
        contexts.append(
            {
                "id": context["id"],
                "path": relative(workspace, context_path),
                "sha256": sha256_path(context_path),
                "sources": sorted(sources, key=lambda value: value["path"]),
            }
        )

    relations = list(spec.get("relationships", [])) + generated_relations
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": True,
        "do_not_edit": "Run: python3 memkit.py refresh",
        "spec_sha256": sha256_json(spec),
        "contexts": sorted(contexts, key=lambda value: value["id"]),
        "relations": sorted(
            relations,
            key=lambda value: (
                value.get("from", ""),
                value.get("type", ""),
                value.get("to", ""),
                value.get("scope", ""),
            ),
        ),
    }


def write_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def relation_issues(workspace: Path, relations: List[Dict[str, Any]]) -> List[str]:
    issues: List[str] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    for relation in relations:
        relation_type = relation.get("type")
        source = relation.get("from", "")
        target = relation.get("to", "")
        scope = relation.get("scope", "")
        key = (source, str(relation_type), target, scope)
        if key in seen:
            issues.append(f"duplicate relation: {key}")
        seen.add(key)
        if relation_type not in ALLOWED_RELATION_TYPES:
            issues.append(f"unknown relation type {relation_type!r}: {source} -> {target}")
        for label, raw in (("from", source), ("to", target)):
            try:
                path = workspace_path(workspace, raw)
            except (TypeError, ValueError) as exc:
                issues.append(f"invalid relation {label}: {raw!r} ({exc})")
                continue
            if not path.is_file():
                issues.append(f"relation {label} missing: {raw}")
        if source == target:
            issues.append(f"self relation is not allowed: {source} ({relation_type})")
        if relation_type == "contradicts":
            resolution = relation.get("resolution")
            if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
                issues.append(f"unresolved contradiction: {source} -> {target} ({scope})")
            elif resolution.get("winner", "") not in {source, target}:
                issues.append(
                    f"contradiction winner must be one endpoint: {source} -> {target}"
                )
    return issues


def link_issues(
    workspace: Path, spec: Dict[str, Any], named: Dict[str, List[Path]]
) -> List[str]:
    allowed, issues = missing_link_exceptions(spec)
    slugged = all_slugged_memories(named)
    prefixes = spec.get("wiki_link_prefixes")
    for root_item in spec["memory_roots"]:
        root = workspace_path(workspace, root_item["path"])
        if not root.is_dir():
            issues.append(f"memory root missing: {root_item['path']}")
            continue
        for source in sorted(root.rglob("*.md")):
            if not source.is_file():
                continue
            text = source.read_text(encoding="utf-8", errors="replace")
            for raw_target in MARKDOWN_LINK_RE.findall(text):
                target = raw_target.strip().strip("<>").split("#", 1)[0]
                target = target.split(maxsplit=1)[0]
                if not target or "://" in target or target.startswith(("mailto:", "/")):
                    continue
                if not target.lower().endswith(".md"):
                    continue
                resolved = (source.parent / target).resolve()
                key = f"{relative(workspace, source)}->{target}"
                if not resolved.is_file() and key not in allowed:
                    issues.append(f"broken markdown link: {key}")
            for raw_target in WIKI_LINK_RE.findall(text):
                target = raw_target.strip()
                if prefixes is not None:
                    if not any(
                        target.startswith(f"{prefix}-") or target.startswith(f"{prefix}_")
                        for prefix in prefixes
                    ):
                        continue
                elif not SLUG_RE.match(target):
                    continue
                basename = target if target.endswith(".md") else f"{target}.md"
                key = f"{relative(workspace, source)}->[[{target}]]"
                exists = bool(named.get(basename) or slugged.get(memory_slug(target)))
                if not exists and key not in allowed:
                    issues.append(f"broken wiki link: {key}")
    return issues


def orphan_issues(workspace: Path, spec: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    archive = archive_dir_name(spec)
    for root_item in spec["memory_roots"]:
        indexes = root_item.get("indexes", [])
        if not indexes:
            continue
        root = workspace_path(workspace, root_item["path"])
        index_paths = [root / name for name in indexes]
        missing = [str(path) for path in index_paths if not path.is_file()]
        if missing:
            issues.extend(f"index missing: {path}" for path in missing)
            continue
        indexed_names: Set[str] = set()
        for index_path in index_paths:
            text = index_path.read_text(encoding="utf-8", errors="replace")
            for raw_target in MARKDOWN_LINK_RE.findall(text):
                target = raw_target.strip().strip("<>").split("#", 1)[0]
                target = target.split(maxsplit=1)[0]
                if target.lower().endswith(".md"):
                    indexed_names.add(Path(target).name)
        index_names = {path.name for path in index_paths}
        allowed = set(root_item.get("orphan_allowlist", []))
        for path in memory_files(root, archive):
            if path.name in index_names or path.name in allowed:
                continue
            if path.name not in indexed_names:
                issues.append(f"orphan memory: {relative(workspace, path)}")
    return issues


def duplicate_issues(workspace: Path, spec: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    archive = archive_dir_name(spec)
    allowed_groups = {
        tuple(sorted(group)) for group in spec.get("allowed_exact_duplicates", [])
    }
    for root_item in spec["memory_roots"]:
        if not root_item.get("check_exact_duplicates", False):
            continue
        root = workspace_path(workspace, root_item["path"])
        by_hash: Dict[str, List[str]] = defaultdict(list)
        index_names = set(root_item.get("indexes", []))
        for path in memory_files(root, archive):
            if path.name in index_names:
                continue
            by_hash[sha256_path(path)].append(relative(workspace, path))
        for paths in by_hash.values():
            if len(paths) < 2:
                continue
            group = tuple(sorted(paths))
            if group not in allowed_groups:
                issues.append(f"exact duplicate memories: {', '.join(group)}")
    return issues


def structural_issues(
    workspace: Path, spec: Dict[str, Any], manifest: Dict[str, Any]
) -> List[str]:
    named = all_named_memories(workspace, spec["memory_roots"])
    issues: List[str] = []
    issues.extend(relation_issues(workspace, manifest.get("relations", [])))
    issues.extend(link_issues(workspace, spec, named))
    issues.extend(orphan_issues(workspace, spec))
    issues.extend(duplicate_issues(workspace, spec))
    return sorted(set(issues))


# --------------------------------------------------------------------------
# Symlink wiring (dry-run, timestamped backup, idempotent)
# --------------------------------------------------------------------------

def expand_target(raw: str, workspace: Optional[Path] = None) -> Path:
    """Expand ``~`` and resolve relative targets against the workspace."""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() and workspace is not None:
        path = workspace / path
    return path


def apply_links(
    workspace: Path,
    spec: Dict[str, Any],
    dry_run: bool,
    backup_root: Optional[Path] = None,
    now: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Wire spec["links"] targets to canonical sources. Returns (log, issues)."""
    log: List[str] = []
    issues: List[str] = []
    entries = spec.get("links", [])
    if not entries:
        return ["no links declared in spec"], issues
    stamp = now or datetime.now().strftime("%Y%m%d_%H%M%S")
    backups = (backup_root or expand_target(
        spec.get("link_backup_dir", "~/.memkit_backups"), workspace
    )) / stamp

    for entry in entries:
        source = workspace_path(workspace, entry["source"])
        target = expand_target(entry["target"], workspace)
        if not source.exists():
            issues.append(f"link source missing: {entry['source']}")
            continue
        if target.is_symlink():
            current = Path(os.readlink(target))
            resolved = (target.parent / current).resolve() if not current.is_absolute() else current.resolve()
            if resolved == source.resolve():
                log.append(f"OK (already linked): {target} -> {source}")
                continue
            log.append(f"RELINK: {target} (was -> {resolved})")
            if not dry_run:
                target.unlink()
        elif target.exists():
            backup_to = backups / target.name
            log.append(f"BACKUP: {target} -> {backup_to}")
            if not dry_run:
                backups.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup_to))
        else:
            log.append(f"NEW: {target}")
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
        log.append(f"{'DRY ' if dry_run else ''}LINK: {target} -> {source}")
    return log, issues


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summarize(manifest: Dict[str, Any]) -> List[str]:
    contexts = manifest["contexts"]
    return [
        "MEMKIT_CHECK=PASS",
        f"CONTEXTS={len(contexts)}",
        f"PROVENANCE_SOURCES={sum(len(item['sources']) for item in contexts)}",
        f"TYPED_RELATIONS={len(manifest['relations'])}",
        "EXTERNAL_API_CALLS=0",
    ]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="memkit", description=__doc__)
    parser.add_argument("command", choices=("check", "refresh", "link"), nargs="?", default="check")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--spec", type=Path, default=None,
                        help="default: <workspace>/memory_spec.json")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="default: spec's manifest_path or <workspace>/memory_manifest.generated.json")
    parser.add_argument("--dry-run", action="store_true", help="link: show plan only")
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    spec_path = (args.spec or workspace / "memory_spec.json").resolve()
    try:
        spec = load_spec(spec_path)
        manifest_path = (
            args.manifest
            or workspace_path(workspace, spec.get("manifest_path", "memory_manifest.generated.json"))
        )
        expected = generate_manifest(workspace, spec)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MEMKIT_CHECK=FAIL\n- {exc}", file=sys.stderr)
        return 1

    if args.command == "link":
        log, issues = apply_links(workspace, spec, dry_run=args.dry_run)
        for line in log:
            print(line)
        if issues:
            print("MEMKIT_LINK=FAIL")
            for issue in issues:
                print(f"- {issue}")
            return 1
        print("MEMKIT_LINK=DRY-RUN-OK" if args.dry_run else "MEMKIT_LINK=OK")
        return 0

    if args.command == "refresh":
        write_manifest(Path(manifest_path), expected)
        print(f"MANIFEST_REFRESHED={relative(workspace, Path(manifest_path))}")

    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        print("MEMKIT_CHECK=FAIL\n- generated manifest missing; run refresh", file=sys.stderr)
        return 1
    try:
        actual = read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MEMKIT_CHECK=FAIL\n- cannot read manifest: {exc}", file=sys.stderr)
        return 1

    issues: List[str] = []
    if actual != expected:
        issues.append("generated manifest is stale; run refresh and review the diff")
    issues.extend(structural_issues(workspace, spec, expected))

    if issues:
        print("MEMKIT_CHECK=FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1

    for line in summarize(expected):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
