#!/usr/bin/env python3
"""Fault-injection calibration for memkit.

A checker is only trustworthy once it has been proven to detect known
defects.  Each test copies the shipped sample workspace to an isolated
temporary directory, proves the healthy baseline PASSes, injects exactly
one defect, proves the check FAILs with the expected message, restores the
workspace, and proves the tree is byte-identical to the baseline again
(PASS + FAIL + clean restore = calibrated).

Injection happens only in the isolated copy — never in a shared tree.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memkit  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "demo"


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(memkit.normalized_bytes(path))
    return digest.hexdigest()


def run(argv: list) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = memkit.main(argv)
    return code, out.getvalue() + err.getvalue()


class FaultInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "demo"
        shutil.copytree(SAMPLE, self.ws)
        code, output = run(["refresh", "--workspace", str(self.ws)])
        self.assertEqual(code, 0, output)
        self.baseline = tree_hash(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self) -> tuple:
        return run(["check", "--workspace", str(self.ws)])

    def assert_calibrated(self, mutate, restore, expected_message: str) -> None:
        """PASS -> inject -> FAIL(with message) -> restore -> PASS + tree identical."""
        code, output = self.check()
        self.assertEqual(code, 0, f"baseline must PASS first:\n{output}")
        mutate()
        code, output = self.check()
        self.assertEqual(code, 1, "defect was not detected")
        self.assertIn(expected_message, output)
        restore()
        code, output = self.check()
        self.assertEqual(code, 0, f"restore did not return to PASS:\n{output}")
        self.assertEqual(tree_hash(self.ws), self.baseline, "restore is not byte-identical")

    def _swap_file(self, rel: str, new_text: str):
        path = self.ws / rel
        original = path.read_bytes()

        def mutate() -> None:
            path.write_text(new_text, encoding="utf-8")

        def restore() -> None:
            path.write_bytes(original)

        return mutate, restore

    def test_detects_stale_source_hash(self) -> None:
        mutate, restore = self._swap_file(
            "memory/feedback_scale_recipes_by_weight.md", "# silently edited\n"
        )
        self.assert_calibrated(mutate, restore, "manifest is stale")

    def test_detects_broken_markdown_link(self) -> None:
        mutate, restore = self._swap_file(
            "memory/project_sourdough_tracker.md",
            "# tracker\nSee [gone](reference_deleted_file.md).\n",
        )
        self.assert_calibrated(mutate, restore, "broken markdown link")

    def test_detects_broken_wiki_link(self) -> None:
        mutate, restore = self._swap_file(
            "memory/project_sourdough_tracker.md",
            "# tracker\nSee [[feedback-rule-that-never-existed]].\n",
        )
        self.assert_calibrated(mutate, restore, "broken wiki link")

    def test_detects_orphan(self) -> None:
        orphan = self.ws / "memory" / "project_unlisted.md"

        def mutate() -> None:
            orphan.write_text("# not in any index\n", encoding="utf-8")

        def restore() -> None:
            orphan.unlink()

        self.assert_calibrated(mutate, restore, "orphan memory")

    def test_detects_exact_duplicate(self) -> None:
        source = self.ws / "memory" / "feedback_never_leave_oven_unattended.md"
        dupe = self.ws / "memory" / "_archive" / "feedback_duplicate_copy.md"
        index = self.ws / "memory" / "MEMORY.md"
        original_index = index.read_bytes()

        def mutate() -> None:
            dupe.write_bytes(source.read_bytes())
            index.write_bytes(
                original_index
                + b"- [dupe](_archive/feedback_duplicate_copy.md)\n"
            )

        def restore() -> None:
            dupe.unlink()
            index.write_bytes(original_index)

        self.assert_calibrated(mutate, restore, "exact duplicate memories")

    def test_detects_manifest_tampering(self) -> None:
        manifest = self.ws / "generated" / "memory_manifest.json"
        original = manifest.read_bytes()

        def mutate() -> None:
            data = json.loads(original)
            data["contexts"][0]["sources"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

        def restore() -> None:
            manifest.write_bytes(original)

        self.assert_calibrated(mutate, restore, "manifest is stale")

    def test_detects_unknown_relation_type_in_spec(self) -> None:
        spec_path = self.ws / "memory_spec.json"
        original = spec_path.read_bytes()

        def mutate() -> None:
            spec = json.loads(original)
            spec["relationships"].append(
                {"from": "memory/ctx_bakery_ops.md", "type": "related_to",
                 "to": "memory/project_sourdough_tracker.md"}
            )
            spec_path.write_text(json.dumps(spec, indent=2))
            run(["refresh", "--workspace", str(self.ws)])

        def restore() -> None:
            spec_path.write_bytes(original)
            run(["refresh", "--workspace", str(self.ws)])

        self.assert_calibrated(mutate, restore, "unknown relation type")

    def test_detects_unresolved_contradiction(self) -> None:
        spec_path = self.ws / "memory_spec.json"
        original = spec_path.read_bytes()

        def mutate() -> None:
            spec = json.loads(original)
            for relation in spec["relationships"]:
                relation.pop("resolution", None)
            spec_path.write_text(json.dumps(spec, indent=2))
            run(["refresh", "--workspace", str(self.ws)])

        def restore() -> None:
            spec_path.write_bytes(original)
            run(["refresh", "--workspace", str(self.ws)])

        self.assert_calibrated(mutate, restore, "unresolved contradiction")

    def test_detects_missing_context_source(self) -> None:
        victim = self.ws / "memory" / "reference_flour_supplier_api.md"
        original = victim.read_bytes()

        def mutate() -> None:
            victim.unlink()

        def restore() -> None:
            victim.write_bytes(original)

        code, output = self.check()
        self.assertEqual(code, 0, output)
        mutate()
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("missing", output)
        restore()
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertEqual(tree_hash(self.ws), self.baseline)


if __name__ == "__main__":
    unittest.main()
