#!/usr/bin/env python3
"""Unit tests for memkit (stdlib only)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memkit  # noqa: E402


def make_min_workspace(root: Path) -> dict:
    memory = root / "memory"
    (memory / "_archive").mkdir(parents=True)
    (memory / "MEMORY.md").write_text(
        "# Index\n- [ctx](ctx_demo.md)\n- [fact](project_fact.md)\n"
        "- [old](_archive/project_old.md)\n",
        encoding="utf-8",
    )
    (memory / "ctx_demo.md").write_text("# ctx\nSee [fact](project_fact.md).\n", encoding="utf-8")
    (memory / "project_fact.md").write_text("# fact\n", encoding="utf-8")
    (memory / "_archive" / "project_old.md").write_text("# old\n", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "manifest_path": "manifest.json",
        "memory_roots": [
            {
                "id": "central",
                "path": "memory",
                "indexes": ["MEMORY.md"],
                "check_exact_duplicates": True,
            }
        ],
        "contexts": [
            {
                "id": "demo",
                "path": "memory/ctx_demo.md",
                "sources": [{"path": "memory/project_fact.md", "role": "evidence"}],
            }
        ],
        "relationships": [],
        "known_missing_link_exceptions": [],
        "allowed_exact_duplicates": [],
    }
    (root / "memory_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return spec


def run_cli(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = memkit.main(argv)
    return code, out.getvalue(), err.getvalue()


class ManifestTest(unittest.TestCase):
    def test_deterministic_and_detects_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            first = memkit.generate_manifest(root, spec)
            second = memkit.generate_manifest(root, spec)
            self.assertEqual(first, second)
            self.assertEqual(first["relations"][0]["type"], "derived_from")
            (root / "memory" / "project_fact.md").write_text("# changed\n", encoding="utf-8")
            changed = memkit.generate_manifest(root, spec)
            self.assertNotEqual(
                first["contexts"][0]["sources"][0]["sha256"],
                changed["contexts"][0]["sources"][0]["sha256"],
            )

    def test_crlf_normalisation_keeps_hash_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.md"
            b = root / "b.md"
            a.write_bytes(b"line1\nline2\n")
            b.write_bytes(b"line1\r\nline2\r\n")
            self.assertEqual(memkit.sha256_path(a), memkit.sha256_path(b))

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                memkit.workspace_path(Path(tmp), "../outside.md")


class RelationTest(unittest.TestCase):
    def _files(self, root: Path) -> None:
        (root / "a.md").write_text("a\n", encoding="utf-8")
        (root / "b.md").write_text("b\n", encoding="utf-8")

    def test_unknown_type_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._files(root)
            issues = memkit.relation_issues(root, [{"from": "a.md", "type": "related", "to": "b.md"}])
            self.assertTrue(any("unknown relation type" in i for i in issues))

    def test_unresolved_contradiction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._files(root)
            issues = memkit.relation_issues(
                root, [{"from": "a.md", "type": "contradicts", "to": "b.md"}]
            )
            self.assertTrue(any("unresolved contradiction" in i for i in issues))

    def test_contradiction_winner_must_be_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._files(root)
            issues = memkit.relation_issues(
                root,
                [{
                    "from": "a.md", "type": "contradicts", "to": "b.md",
                    "resolution": {"status": "resolved", "winner": "c.md"},
                }],
            )
            self.assertTrue(any("winner must be one endpoint" in i for i in issues))

    def test_self_and_duplicate_relations_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._files(root)
            rel = {"from": "a.md", "type": "depends_on", "to": "a.md"}
            issues = memkit.relation_issues(root, [rel, dict(rel)])
            self.assertTrue(any("self relation" in i for i in issues))
            self.assertTrue(any("duplicate relation" in i for i in issues))


class LinkCheckTest(unittest.TestCase):
    def test_wiki_link_matches_hyphen_underscore_and_frontmatter_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory"
            memory.mkdir()
            (memory / "project_src.md").write_text(
                "See [[feedback-stay-calm]] and [[project-renamed]].\n", encoding="utf-8"
            )
            (memory / "feedback_stay_calm.md").write_text("# rule\n", encoding="utf-8")
            (memory / "project_other_20260101.md").write_text(
                "---\nname: project-renamed\n---\n# renamed\n", encoding="utf-8"
            )
            spec = {"memory_roots": [{"id": "c", "path": "memory"}]}
            named = memkit.all_named_memories(root, spec["memory_roots"])
            self.assertEqual(memkit.link_issues(root, spec, named), [])

    def test_exception_requires_reason(self) -> None:
        allowed, issues = memkit.missing_link_exceptions(
            {"known_missing_link_exceptions": [{"reference": "a.md->b.md", "reason": ""}]}
        )
        self.assertEqual(allowed, set())
        self.assertTrue(any("reason required" in i for i in issues))


class OrphanDuplicateTest(unittest.TestCase):
    def test_orphan_and_duplicate_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            (root / "memory" / "project_orphan.md").write_text("# lonely\n", encoding="utf-8")
            (root / "memory" / "project_dupe.md").write_text("# fact\n", encoding="utf-8")
            orphans = memkit.orphan_issues(root, spec)
            self.assertTrue(any("project_orphan.md" in i for i in orphans))
            dups = memkit.duplicate_issues(root, spec)
            self.assertTrue(any("exact duplicate" in i for i in dups))


class SymlinkTest(unittest.TestCase):
    def _spec(self, root: Path) -> dict:
        spec = make_min_workspace(root)
        spec["links"] = [{"source": "memory", "target": "live/memory"}]
        spec["link_backup_dir"] = "backups"
        (root / "memory_spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return spec

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec(root)
            log, issues = memkit.apply_links(root, spec, dry_run=True)
            self.assertEqual(issues, [])
            self.assertFalse((root / "live").exists())

    def test_link_backup_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec(root)
            live = root / "live" / "memory"
            live.mkdir(parents=True)
            (live / "keep_me.md").write_text("precious\n", encoding="utf-8")

            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertEqual(issues, [])
            self.assertTrue(live.is_symlink())
            ledger = json.loads(
                (root / "backups" / "t1" / "restore_ledger.json").read_text()
            )
            backup = Path(ledger["restore"][0]["backup"]) / "keep_me.md"
            self.assertEqual(backup.read_text(encoding="utf-8"), "precious\n")

            log2, issues2 = memkit.apply_links(root, spec, dry_run=False, now="t2")
            self.assertEqual(issues2, [])
            self.assertTrue(any("already linked" in line for line in log2))
            self.assertFalse((root / "backups" / "t2").exists())

    def test_wrong_symlink_is_retargeted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = self._spec(root)
            other = root / "other"
            other.mkdir()
            live = root / "live" / "memory"
            live.parent.mkdir(parents=True)
            live.symlink_to(other)
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertEqual(issues, [])
            self.assertEqual(live.resolve(), (root / "memory").resolve())
            self.assertTrue(any("RELINK" in line for line in log))


class PreflightTest(unittest.TestCase):
    """Every dangerous link configuration must be rejected before any write."""

    def _run_link(self, root: Path, links: list) -> tuple:
        spec = make_min_workspace(root)
        spec["links"] = links
        spec["link_backup_dir"] = "backups"
        (root / "memory_spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return memkit.apply_links(root, spec, dry_run=False, now="t1")

    def _assert_rejected(self, root: Path, links: list, message: str) -> None:
        log, issues = self._run_link(root, links)
        self.assertTrue(any(message in i for i in issues), f"{message} not raised: {issues}")
        self.assertFalse((root / "backups").exists(), "preflight must not write anything")

    def test_rejects_workspace_root_and_filesystem_root_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(root, [{"source": "memory", "target": str(root)}],
                                  "dangerous target")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(root, [{"source": "memory", "target": "/"}],
                                  "dangerous target")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(root, [{"source": "memory", "target": str(Path.home())}],
                                  "dangerous target")

    def test_rejects_source_target_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(root, [{"source": "memory", "target": "memory/_archive"}],
                                  "its own source")

    def test_rejects_duplicate_and_nested_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(
                root,
                [{"source": "memory", "target": "live/a"},
                 {"source": "memory", "target": "live/a"}],
                "duplicate target",
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._assert_rejected(
                root,
                [{"source": "memory", "target": "live/a"},
                 {"source": "memory", "target": "live/a/nested"}],
                "overlaps another target",
            )

    def test_rejects_backup_collision_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "live" / "memory").mkdir(parents=True)
            normalized_target = (root / "live").resolve() / "memory"
            collision = root / "backups" / "t1" / memkit.backup_name_for(normalized_target)
            collision.mkdir(parents=True)
            log, issues = self._run_link(
                root, [{"source": "memory", "target": "live/memory"}]
            )
            self.assertTrue(any("backup collision" in i for i in issues))
            self.assertFalse((root / "live" / "memory").is_symlink(),
                             "nothing may be linked when preflight fails")

    def test_one_bad_entry_blocks_all_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, issues = self._run_link(
                root,
                [{"source": "memory", "target": "live/good"},
                 {"source": "memory", "target": "/"}],
            )
            self.assertTrue(issues)
            self.assertFalse((root / "live").exists(),
                             "valid entries must not run when any entry is rejected")


class RollbackLedgerTest(unittest.TestCase):
    def test_mid_failure_rolls_back_links_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [
                {"source": "memory", "target": "live/a"},
                {"source": "memory", "target": "live/b"},
            ]
            spec["link_backup_dir"] = "backups"
            live_b = root / "live" / "b"
            live_b.mkdir(parents=True)
            (live_b / "precious.md").write_text("keep\n", encoding="utf-8")

            original_symlink_to = Path.symlink_to
            calls = {"n": 0}

            def failing_symlink_to(self_path, target_path):  # noqa: ANN001
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("injected failure on second link")
                return original_symlink_to(self_path, target_path)

            Path.symlink_to = failing_symlink_to  # type: ignore[method-assign]
            try:
                log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            finally:
                Path.symlink_to = original_symlink_to  # type: ignore[method-assign]

            self.assertTrue(any("rolled back" in i for i in issues))
            self.assertFalse((root / "live" / "a").is_symlink(), "created link must be removed")
            self.assertTrue((live_b / "precious.md").is_file(), "backup must be restored")
            self.assertFalse(live_b.is_symlink())

    def test_restore_ledger_written_with_full_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [{"source": "memory", "target": "live/memory"}]
            spec["link_backup_dir"] = "backups"
            live = root / "live" / "memory"
            live.mkdir(parents=True)
            (live / "old.md").write_text("old\n", encoding="utf-8")
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertEqual(issues, [])
            ledger = json.loads(
                (root / "backups" / "t1" / "restore_ledger.json").read_text()
            )
            self.assertEqual(len(ledger["restore"]), 1)
            entry = ledger["restore"][0]
            self.assertTrue(entry["original"].endswith("live/memory"))
            self.assertTrue(Path(entry["backup"]).is_dir())


class NewCheckSemanticsTest(unittest.TestCase):
    def test_ambiguous_wiki_link_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory"
            memory.mkdir()
            (memory / "project_src.md").write_text("See [[feedback-one-rule]].\n", encoding="utf-8")
            (memory / "feedback_one_rule.md").write_text("# a\n", encoding="utf-8")
            (memory / "feedback-one-rule.md").write_text("# b\n", encoding="utf-8")
            spec = {"memory_roots": [{"id": "c", "path": "memory"}]}
            named = memkit.all_named_memories(root, spec["memory_roots"])
            issues = memkit.link_issues(root, spec, named)
            self.assertTrue(any("ambiguous wiki link" in i for i in issues), issues)

    def test_orphan_uses_paths_not_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            shadow = root / "memory" / "_archive" / "project_fact.md"
            shadow.write_text("# same basename, different file\n", encoding="utf-8")
            issues = memkit.orphan_issues(root, spec)
            self.assertTrue(
                any("_archive/project_fact.md" in i for i in issues),
                f"basename shadowing must not hide orphans: {issues}",
            )

    def test_cross_root_exact_duplicates_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            other = root / "silo"
            other.mkdir()
            (other / "project_copy.md").write_text("# fact\n", encoding="utf-8")
            spec["memory_roots"].append(
                {"id": "silo", "path": "silo", "check_exact_duplicates": True}
            )
            issues = memkit.duplicate_issues(root, spec)
            self.assertTrue(
                any("memory/project_fact.md" in i and "silo/project_copy.md" in i
                    for i in issues),
                issues,
            )


def _fs_case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe_memkit"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe_memkit").exists()
    finally:
        probe.unlink()


class TransactionSafetyTest(unittest.TestCase):
    """Fault injections for the five hardening classes of review round 2."""

    def test_manifest_symlink_escape_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "ws"
            ws.mkdir()
            make_min_workspace(ws)
            outside = base / "outside"
            outside.mkdir()
            (ws / "escape").symlink_to(outside)
            code, out, err = run_cli(
                ["refresh", "--workspace", str(ws),
                 "--manifest", str(ws / "escape" / "m.json")]
            )
            self.assertEqual(code, 1)
            self.assertIn("must stay inside the workspace", err)
            self.assertEqual(list(outside.iterdir()), [],
                             "nothing may be written through the symlink")

    def test_case_alias_of_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if not _fs_case_insensitive(root):
                self.skipTest("requires a case-insensitive filesystem")
            spec = make_min_workspace(root)
            spec["links"] = [{"source": "memory", "target": "Memory"}]
            spec["link_backup_dir"] = "backups"
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(
                any("aliases its own source" in i or "dangerous target" in i
                    for i in issues),
                issues,
            )
            self.assertTrue((root / "memory" / "MEMORY.md").is_file(),
                            "source must remain untouched")

    def test_backup_names_cannot_collide_for_tricky_paths(self) -> None:
        a = memkit.backup_name_for(Path("/a/b__c"))
        b = memkit.backup_name_for(Path("/a/b/c"))
        self.assertNotEqual(a, b, "separator-substitution collision is back")

    def test_planned_backup_duplicate_is_rejected_in_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            for name in ("a", "b"):
                real = root / "live" / name
                real.mkdir(parents=True)
                (real / "data.md").write_text("x\n", encoding="utf-8")
            spec["links"] = [
                {"source": "memory", "target": "live/a"},
                {"source": "memory", "target": "live/b"},
            ]
            spec["link_backup_dir"] = "backups"
            original_namer = memkit.backup_name_for
            memkit.backup_name_for = lambda target: "constant"  # type: ignore[assignment]
            try:
                log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            finally:
                memkit.backup_name_for = original_namer  # type: ignore[assignment]
            self.assertTrue(any("planned backup destination collides" in i for i in issues))
            self.assertFalse((root / "backups").exists())

    def test_backup_root_overlapping_target_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [{"source": "memory", "target": "live/memory"}]
            spec["link_backup_dir"] = "live/memory/backups"
            (root / "live" / "memory").mkdir(parents=True)
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(any("backup directory overlaps" in i for i in issues))
            self.assertFalse((root / "live" / "memory").is_symlink())

    def test_failed_relink_restores_original_symlink_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [{"source": "memory", "target": "live/memory"}]
            spec["link_backup_dir"] = "backups"
            other = root / "other"
            other.mkdir()
            live = root / "live" / "memory"
            live.parent.mkdir(parents=True)
            live.symlink_to(other)
            old_text = os.readlink(live)

            original_symlink_to = Path.symlink_to

            def always_fail(self_path, target_path):  # noqa: ANN001
                raise OSError("injected: symlink creation denied")

            Path.symlink_to = always_fail  # type: ignore[method-assign]
            try:
                log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            finally:
                Path.symlink_to = original_symlink_to  # type: ignore[method-assign]

            self.assertTrue(any("rolled back" in i for i in issues))
            self.assertTrue(live.is_symlink(), "original symlink must be restored")
            self.assertEqual(os.readlink(live), old_text)

    def test_failed_link_removes_created_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [{"source": "memory", "target": "deep/nested/memory"}]
            spec["link_backup_dir"] = "backups"

            original_symlink_to = Path.symlink_to

            def always_fail(self_path, target_path):  # noqa: ANN001
                raise OSError("injected: symlink creation denied")

            Path.symlink_to = always_fail  # type: ignore[method-assign]
            try:
                log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            finally:
                Path.symlink_to = original_symlink_to  # type: ignore[method-assign]

            self.assertTrue(any("rolled back" in i for i in issues))
            self.assertFalse((root / "deep").exists(),
                             "created parent directories must be removed")

    def test_cross_entry_target_equal_to_other_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            src_b = root / "srcB"
            src_b.mkdir()
            (src_b / "x.md").write_text("x\n", encoding="utf-8")
            spec["links"] = [
                {"source": "memory", "target": "srcB"},
                {"source": "srcB", "target": "live/b"},
            ]
            spec["link_backup_dir"] = "backups"
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(any("the source of links[1]" in i for i in issues), issues)
            self.assertFalse(src_b.is_symlink(), "other entry's source must stay intact")
            self.assertFalse((root / "live").exists())

    def test_cross_entry_target_ancestor_of_other_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            deep_src = root / "pool" / "srcB"
            deep_src.mkdir(parents=True)
            (deep_src / "x.md").write_text("x\n", encoding="utf-8")
            spec["links"] = [
                {"source": "memory", "target": "pool"},
                {"source": "pool/srcB", "target": "live/b"},
            ]
            spec["link_backup_dir"] = "backups"
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(any("the source of links[1]" in i for i in issues), issues)
            self.assertFalse((root / "pool").is_symlink())

    def test_cross_entry_target_descendant_of_other_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            src_b = root / "srcB"
            src_b.mkdir()
            (src_b / "x.md").write_text("x\n", encoding="utf-8")
            spec["links"] = [
                {"source": "memory", "target": "srcB/inner"},
                {"source": "srcB", "target": "live/b"},
            ]
            spec["link_backup_dir"] = "backups"
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(any("the source of links[1]" in i for i in issues), issues)
            self.assertFalse((src_b / "inner").exists())

    def test_portable_case_duplicate_targets_rejected_everywhere(self) -> None:
        # Both targets do not exist yet, so samefile cannot help; the
        # portable (NFC + casefold) key must reject the pair on every
        # filesystem, case-sensitive or not.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [
                {"source": "memory", "target": "live/CaseLink"},
                {"source": "memory", "target": "live/caselink"},
            ]
            spec["link_backup_dir"] = "backups"
            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertTrue(
                any("duplicate target (portable identity" in i for i in issues), issues
            )
            self.assertFalse((root / "live").exists(),
                             "one rejection must block all writes")

    def test_transaction_journal_records_relink_and_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_min_workspace(root)
            spec["links"] = [
                {"source": "memory", "target": "live/wrong"},
                {"source": "memory", "target": "live/real"},
            ]
            spec["link_backup_dir"] = "backups"
            other = root / "other"
            other.mkdir()
            wrong = root / "live" / "wrong"
            wrong.parent.mkdir(parents=True)
            wrong.symlink_to(other)
            real = root / "live" / "real"
            real.mkdir()
            (real / "x.md").write_text("x\n", encoding="utf-8")

            log, issues = memkit.apply_links(root, spec, dry_run=False, now="t1")
            self.assertEqual(issues, [])
            journal = json.loads(
                (root / "backups" / "t1" / "transaction_journal.json").read_text()
            )
            self.assertEqual(len(journal["relinked"]), 1)
            self.assertTrue(journal["relinked"][0]["old_link"].endswith("other"))
            self.assertEqual(len(journal["moved"]), 1)
            self.assertEqual(len(journal["created_links"]), 2)


class CliTest(unittest.TestCase):
    def test_check_requires_refresh_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_min_workspace(root)
            code, out, err = run_cli(["check", "--workspace", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("manifest missing", err)
            code, out, err = run_cli(["refresh", "--workspace", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("MEMKIT_CHECK=PASS", out)

    def test_manifest_outside_workspace_fails_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            make_min_workspace(root)
            outside = Path(tmp) / "evil_manifest.json"
            code, out, err = run_cli(
                ["refresh", "--workspace", str(root), "--manifest", str(outside)]
            )
            self.assertEqual(code, 1)
            self.assertIn("must stay inside the workspace", err)
            self.assertFalse(outside.exists(), "no file may be written outside the workspace")


if __name__ == "__main__":
    unittest.main()
