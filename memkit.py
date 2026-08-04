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
import unicodedata
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
                candidates = {
                    path.resolve()
                    for path in (
                        list(named.get(basename, []))
                        + list(slugged.get(memory_slug(target), []))
                    )
                }
                if not candidates and key not in allowed:
                    issues.append(f"broken wiki link: {key}")
                elif len(candidates) > 1:
                    listing = ", ".join(
                        sorted(relative(workspace, path) for path in candidates)
                    )
                    issues.append(
                        f"ambiguous wiki link ({len(candidates)} matches): {key} -> {listing}"
                    )
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
        indexed_paths: Set[str] = set()
        for index_path in index_paths:
            text = index_path.read_text(encoding="utf-8", errors="replace")
            for raw_target in MARKDOWN_LINK_RE.findall(text):
                target = raw_target.strip().strip("<>").split("#", 1)[0]
                target = target.split(maxsplit=1)[0]
                if not target.lower().endswith(".md") or "://" in target:
                    continue
                resolved = Path(os.path.abspath(index_path.parent / target))
                try:
                    indexed_paths.add(resolved.relative_to(root.resolve()).as_posix())
                except ValueError:
                    continue  # index may legitimately link outside this root
        index_rels = {
            Path(os.path.abspath(path)).relative_to(root.resolve()).as_posix()
            for path in index_paths
        }
        allowed = {
            Path(entry).as_posix() for entry in root_item.get("orphan_allowlist", [])
        }
        for path in memory_files(root, archive):
            rel_to_root = path.resolve().relative_to(root.resolve()).as_posix()
            if rel_to_root in index_rels or rel_to_root in allowed:
                continue
            if rel_to_root not in indexed_paths:
                issues.append(f"orphan memory: {relative(workspace, path)}")
    return issues


def duplicate_issues(workspace: Path, spec: Dict[str, Any]) -> List[str]:
    """Exact-content duplicates, pooled across every opted-in memory root."""
    issues: List[str] = []
    archive = archive_dir_name(spec)
    allowed_groups = {
        tuple(sorted(group)) for group in spec.get("allowed_exact_duplicates", [])
    }
    by_hash: Dict[str, List[str]] = defaultdict(list)
    for root_item in spec["memory_roots"]:
        if not root_item.get("check_exact_duplicates", False):
            continue
        root = workspace_path(workspace, root_item["path"])
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
# Symlink wiring: preflight (validate everything) -> execute (with rollback)
# --------------------------------------------------------------------------

def expand_target(raw: str, workspace: Optional[Path] = None) -> Path:
    """Expand ``~`` and resolve relative targets against the workspace."""
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute() and workspace is not None:
        path = workspace / path
    return Path(os.path.abspath(path))


def _is_same_or_ancestor(candidate: Path, of: Path) -> bool:
    """True when ``candidate`` equals ``of`` or is one of its ancestors."""
    c = candidate.as_posix().rstrip("/") + "/"
    o = of.as_posix().rstrip("/") + "/"
    return o.startswith(c)


def _overlapping(a: Path, b: Path) -> bool:
    return _is_same_or_ancestor(a, b) or _is_same_or_ancestor(b, a)


def backup_name_for(target: Path) -> str:
    """Collision-resistant backup name: SHA-256 of the absolute path + basename.

    A separator-substitution scheme ("/" -> "__") would collide for
    ``/a/b__c`` vs ``/a/b/c``; a truncated hash makes that practically
    impossible, and preflight additionally detects any collision among
    existing and planned backup destinations before anything is written.
    """
    digest = hashlib.sha256(target.as_posix().encode("utf-8")).hexdigest()[:16]
    return f"{digest}__{target.name or 'root'}"


def portable_key(path: Path) -> str:
    """Filesystem-portable identity key for not-yet-created paths.

    NFC-normalised and casefolded, so ``CaseLink`` and ``caselink`` (or NFD
    vs NFC spellings) are treated as the same future path. This is stricter
    than a case-sensitive filesystem requires, by design: link layouts that
    only work on some filesystems are rejected everywhere.
    """
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _keys_overlap(a: str, b: str) -> bool:
    a_s, b_s = a.rstrip("/") + "/", b.rstrip("/") + "/"
    return a_s.startswith(b_s) or b_s.startswith(a_s)


def _samefile(a: Path, b: Path) -> bool:
    """True when both paths exist and are the same file (case-alias safe)."""
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def preflight_links(
    workspace: Path, spec: Dict[str, Any], backups: Path
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validate every link entry before anything is written.

    Returns (plan, issues). The plan is only executable when issues == [].
    """
    plan: List[Dict[str, Any]] = []
    issues: List[str] = []
    protected = [Path("/"), Path.home().resolve(), workspace.resolve()]
    backups_norm = backups.resolve()

    # ---- Phase A: collect and normalise every entry first ----------------
    parsed: List[Dict[str, Any]] = []
    for index, entry in enumerate(spec.get("links", [])):
        label = f"links[{index}]"
        try:
            source = workspace_path(workspace, entry["source"])
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(f"{label}: invalid source ({exc})")
            continue
        if not source.exists():
            issues.append(f"{label}: link source missing: {entry['source']}")
            continue
        try:
            target = expand_target(entry["target"], workspace)
        except (KeyError, TypeError) as exc:
            issues.append(f"{label}: invalid target ({exc})")
            continue
        # Normalise the parent (follows directory symlinks such as macOS
        # /var -> /private/var) while never following the leaf itself, so
        # an existing wrong symlink is still seen as a symlink.
        target = target.parent.resolve() / target.name
        parsed.append(
            {
                "label": label,
                "source": source,
                "source_abs": source.resolve(),
                "target": target,
                "target_key": portable_key(target),
                "target_is_symlink": target.is_symlink(),
            }
        )

    all_sources = [(item["label"], item["source_abs"]) for item in parsed]

    # ---- Phase B: cross-entry validation, then per-entry classification --
    seen_target_keys: Set[str] = set()
    accepted: List[Dict[str, Any]] = []
    planned_backups: Set[str] = set()

    for item in parsed:
        label = item["label"]
        source: Path = item["source"]
        source_abs: Path = item["source_abs"]
        target: Path = item["target"]
        target_key: str = item["target_key"]
        # samefile checks are for REAL files/dirs only: a symlink target is
        # judged by the noop/relink logic (replacing a symlink loses no data),
        # and samefile would follow it and misfire on the already-linked case.
        target_is_symlink: bool = item["target_is_symlink"]

        rejected = False
        for danger in protected:
            if _is_same_or_ancestor(target, danger) or (
                not target_is_symlink and _samefile(target, danger)
            ):
                issues.append(
                    f"{label}: refusing dangerous target {target} "
                    f"(equals, aliases, or contains {danger})"
                )
                rejected = True
                break
        if rejected:
            continue

        # Every target is checked against EVERY source, not only its own:
        # replacing a directory that contains (or is) another entry's source
        # would corrupt that entry mid-run.
        for other_label, other_source in all_sources:
            if (
                _overlapping(target, other_source)
                or _keys_overlap(target_key, portable_key(other_source))
                or (not target_is_symlink and _samefile(target, other_source))
            ):
                suffix = "its own source" if other_label == label else f"the source of {other_label}"
                issues.append(
                    f"{label}: target overlaps or aliases {suffix}: "
                    f"{target} <-> {other_source}"
                )
                rejected = True
                break
        if rejected:
            continue

        if _overlapping(backups_norm, target) or _overlapping(backups_norm, source_abs):
            issues.append(
                f"{label}: backup directory overlaps a link source/target: "
                f"{backups_norm} <-> {target}"
            )
            continue
        if target_key in seen_target_keys:
            issues.append(
                f"{label}: duplicate target (portable identity, case/NFC "
                f"insensitive): {target}"
            )
            continue
        overlap = next(
            (
                previous
                for previous in accepted
                if _keys_overlap(target_key, previous["target_key"])
                or _overlapping(target, previous["target"])
                or _samefile(target, previous["target"])
            ),
            None,
        )
        if overlap is not None:
            issues.append(
                f"{label}: target overlaps another target: "
                f"{target} <-> {overlap['target']}"
            )
            continue

        action, detail = "link_new", ""
        if target.is_symlink():
            raw_link = os.readlink(target)
            current = Path(raw_link)
            resolved = (
                (target.parent / current).resolve()
                if not current.is_absolute()
                else current.resolve()
            )
            if resolved == source.resolve():
                action = "noop"
            else:
                action, detail = "relink", raw_link
        elif target.exists():
            action = "backup_link"
            backup_dest = backups / backup_name_for(target)
            if backup_dest.exists():
                issues.append(f"{label}: backup collision: {backup_dest}")
                continue
            if str(backup_dest) in planned_backups:
                issues.append(
                    f"{label}: planned backup destination collides: {backup_dest}"
                )
                continue
            planned_backups.add(str(backup_dest))
            detail = str(backup_dest)
        seen_target_keys.add(target_key)
        accepted.append({"target": target, "target_key": target_key})
        plan.append(
            {"action": action, "source": source, "target": target,
             "detail": detail, "label": label}
        )
    return plan, issues


def _write_ledger(backups: Path, moved: List[Tuple[Path, Path]]) -> None:
    ledger = [
        {"original": str(original), "backup": str(backup)}
        for original, backup in moved
    ]
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "restore_ledger.json").write_text(
        json.dumps({"restore": ledger}, indent=2) + "\n", encoding="utf-8"
    )


def _rollback(
    created_links: List[Path],
    created_dirs: List[Path],
    moved: List[Tuple[Path, Path]],
    relinked: List[Tuple[Path, str]],
    log: List[str],
) -> None:
    """Undo a partially executed plan completely, newest change first."""
    for link in reversed(created_links):
        if link.is_symlink():
            link.unlink()
            log.append(f"ROLLBACK unlink: {link}")
    for target, old_link in reversed(relinked):
        if not target.exists() and not target.is_symlink():
            os.symlink(old_link, target)
            log.append(f"ROLLBACK relink restored: {target} -> {old_link}")
    for original, backup in reversed(moved):
        if backup.exists() and not original.exists():
            shutil.move(str(backup), str(original))
            log.append(f"ROLLBACK restore: {backup} -> {original}")
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
            log.append(f"ROLLBACK rmdir: {directory}")
        except OSError:
            pass  # not empty or already gone; never force-delete


def apply_links(
    workspace: Path,
    spec: Dict[str, Any],
    dry_run: bool,
    backup_root: Optional[Path] = None,
    now: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Two-phase link wiring: preflight everything, then execute with rollback."""
    entries = spec.get("links", [])
    if not entries:
        return ["no links declared in spec"], []
    stamp = now or datetime.now().strftime("%Y%m%d_%H%M%S")
    backups = (backup_root or expand_target(
        spec.get("link_backup_dir", "~/.memkit_backups"), workspace
    )) / stamp

    plan, issues = preflight_links(workspace, spec, backups)
    log = [f"PREFLIGHT: {len(plan)} ok / {len(issues)} rejected"]
    if issues:
        return log, issues

    for item in plan:
        prefix = "DRY " if dry_run else ""
        if item["action"] == "noop":
            log.append(f"OK (already linked): {item['target']} -> {item['source']}")
        elif item["action"] == "relink":
            log.append(f"{prefix}RELINK: {item['target']} (was -> {item['detail']})")
        elif item["action"] == "backup_link":
            log.append(f"{prefix}BACKUP: {item['target']} -> {item['detail']}")
        if item["action"] != "noop":
            log.append(f"{prefix}LINK: {item['target']} -> {item['source']}")
    if dry_run:
        return log, issues

    created_links: List[Path] = []
    created_dirs: List[Path] = []
    moved: List[Tuple[Path, Path]] = []
    relinked: List[Tuple[Path, str]] = []

    def write_journal() -> None:
        backups.mkdir(parents=True, exist_ok=True)
        data = {
            "moved": [{"original": str(o), "backup": str(b)} for o, b in moved],
            "relinked": [{"target": str(t), "old_link": raw} for t, raw in relinked],
            "created_links": [str(p) for p in created_links],
            "created_dirs": [str(p) for p in created_dirs],
        }
        (backups / "transaction_journal.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    try:
        for item in plan:
            target: Path = item["target"]
            if item["action"] == "noop":
                continue
            if item["action"] == "relink":
                relinked.append((target, item["detail"]))
                write_journal()
                target.unlink()
            elif item["action"] == "backup_link":
                backup_dest = Path(item["detail"])
                backups.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup_dest))
                moved.append((target, backup_dest))
                _write_ledger(backups, moved)
                write_journal()
            missing_parents: List[Path] = []
            probe = target.parent
            while not probe.exists():
                missing_parents.append(probe)
                probe = probe.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                created_dirs.append(directory)
            if missing_parents:
                write_journal()
            target.symlink_to(item["source"])
            created_links.append(target)
            write_journal()
    except OSError as exc:
        _rollback(created_links, created_dirs, moved, relinked, log)
        issues.append(f"link execution failed and was rolled back: {exc}")
        return log, issues
    if moved:
        _write_ledger(backups, moved)
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
        if args.manifest is not None:
            # Fully resolve (symlinks in every existing component included)
            # BEFORE the containment decision, so a symlink inside the
            # workspace cannot smuggle the write outside it.
            manifest_path = args.manifest.expanduser().resolve()
            try:
                manifest_path.relative_to(workspace)
            except ValueError:
                raise ValueError(
                    f"--manifest must stay inside the workspace: {manifest_path}"
                ) from None
        else:
            manifest_path = workspace_path(
                workspace, spec.get("manifest_path", "memory_manifest.generated.json")
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
