# agent-memory-kit

**Durable, verifiable, file-based long-term memory for AI agents and humans.**
Markdown + Git + symlinks. No database, no embeddings, no LLM calls, no
daemon, no API keys. One stdlib-only Python file.

[![tests](https://img.shields.io/badge/tests-51%20passing-brightgreen)](tests/)
[![zero-api](https://img.shields.io/badge/network%20calls-0%20(test--enforced)-blue)](tests/test_zero_api.py)

## Why

Agent memory tools tend to reach for vector stores, background indexers,
and per-token distillation calls. That stack is expensive, opaque, and
fails silently. This kit takes the opposite bet:

- **Plain Markdown files** are the memory. Readable, diffable, greppable.
- **Git** is the sync and history layer. Your memory travels with your repos.
- **Symlinks** unify the many places a coding agent writes memory into one
  canonical location.
- **Determinism over inference.** Freshness is a SHA-256 comparison, not a
  similarity score. A check either passes or tells you exactly what broke.
- **Automation never overwrites human judgment.** The only file this tool
  writes is a generated manifest (and symlinks). Your Markdown is yours.

## The layer model

| Layer | What it is | Example |
|---|---|---|
| L0 | Raw session transcripts | agent JSONL (never parsed by this tool) |
| L0.5 | Summarised session logs | `logs/2026-01-15.md` |
| L1 | One fact / decision / lesson per file | `feedback_scale_recipes_by_weight.md` |
| L2 | Per-domain distilled context with provenance | `ctx_bakery_ops.md` |
| L3 | Small router/index loaded at session start | `MEMORY.md` |

L2 files carry **provenance**: each claim's source files are declared in a
spec, and their SHA-256 hashes are frozen into a generated manifest. When a
source changes, `check` fails until a human re-reads the source and
re-approves the context. Stale memory is *detected*, not silently trusted.

## Quickstart

```bash
git clone <this-repo> && cd agent-memory-kit
python3 memkit.py check --workspace samples/demo     # PASS on the shipped sample
```

Try breaking it (in a copy):

```bash
cp -R samples/demo /tmp/demo
echo "edited without re-approval" >> /tmp/demo/memory/feedback_scale_recipes_by_weight.md
python3 memkit.py check --workspace /tmp/demo        # FAIL: manifest is stale
```

Set up your own workspace:

```bash
cp spec.template.json my-workspace/memory_spec.json  # edit paths
python3 memkit.py refresh --workspace my-workspace   # generate the manifest
python3 memkit.py check   --workspace my-workspace
```

## CLI

| Command | What it does |
|---|---|
| `memkit.py check` | Verifies manifest freshness (SHA-256), typed relations, Markdown/wiki links, orphans (files missing from every index), and exact-content duplicates. Exit 0 = PASS. |
| `memkit.py refresh` | Deterministically regenerates the manifest from the human-authored spec. Atomic write; safe to run repeatedly. |
| `memkit.py link [--dry-run]` | Wires live directories to their canonical location via symlinks in two phases: **preflight** validates every entry (dangerous targets such as `/`, `$HOME`, the workspace root, source/target overlaps, nested or duplicate targets, and backup collisions are all rejected before anything is written — one bad entry blocks all writes), then **execute** with automatic rollback on mid-failure. Existing real directories are moved to a per-path-unique timestamped backup with a `restore_ledger.json`; re-runs are no-ops. |

## Typed relations

Four relation types, validated on every check:

- `derived_from` — generated automatically from each context's declared sources
- `supersedes` — the new memory replaces an archived one
- `contradicts` — **must** carry a `resolution` with a `winner` that is one
  of the two endpoints; unresolved contradictions fail the check
- `depends_on` — this memory relies on another staying true

## The spec (human) vs the manifest (generated)

`memory_spec.json` is the human boundary: roots, indexes, contexts and
their sources, relationships, narrowly-scoped link exceptions (each one
requires a written reason), and symlink wiring. `refresh` compiles it into
`memory_manifest.generated.json` — which carries a `do_not_edit` marker and
is fully reproducible. Automation and judgment never share a file.

## Safety design

- **Dry-run everything**: `link --dry-run` prints the exact plan.
- **Preflight before write**: every link entry is validated first; any
  rejection (dangerous target incl. case-insensitive aliases via
  `samefile`, source/target overlap, nested or duplicate targets, backup
  directory overlapping a source/target, planned-backup collisions)
  blocks the whole run before a single write happens.
- **Timestamped backups + restore ledger + transaction journal**: a real
  directory at a link target is moved to a backup folder named by the
  SHA-256 of its absolute path (collision free by construction), never
  removed, and recorded in `restore_ledger.json`. Every mutation —
  moves, replaced symlinks (with their original link text), created
  parent directories, created links — is journalled to
  `transaction_journal.json` as it happens; mid-failure triggers a
  complete automatic rollback including restoring replaced symlinks and
  removing created parent directories.
- **Idempotent**: re-running `link` on a wired workspace changes nothing.
- **Path containment**: spec paths may not escape the workspace, and
  `--manifest` is fully symlink-resolved before its containment check —
  a symlink inside the workspace cannot smuggle the write outside.
- **CRLF-stable hashing**: line endings are normalised before hashing, so
  a repo shared between macOS/Linux/WSL doesn't produce false staleness.
- **Zero API, test-enforced**: [`tests/test_zero_api.py`](tests/test_zero_api.py)
  whitelists stdlib imports, forbids process/dynamic-import escape hatches
  (`os.system`, `os.exec*`, `eval`, `__import__`, …), *and* runs the full
  CLI with `socket` replaced by a bomb. Tests are strong evidence, not a
  formal proof.
- **Calibrated checks**: every check dimension has a fault-injection test
  — healthy PASS, injected defect FAIL, byte-identical restore. A checker
  that has never failed on a known defect is not a checker.

## What this is not

- Not a vector database, not RAG, and not a summariser — distillation into
  L2 is deliberately a human-approved act.
- Not an agent framework hook. No init that rewrites your settings, no MCP
  server, no background process.
- Not a sync engine — Git (or any file sync you already trust) does that.

## Acknowledgments

Design ideas (no code) were informed by two MIT-licensed projects:

- [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)
  (MIT) — the layered L0→L3 memory model and agent-scoped visibility.
- [Graft](https://github.com/NanoNets/Graft) (MIT) — deterministic local
  indexing and verification instead of API-dependent context building.

Both are acknowledged for ideas only; this codebase is an independent
implementation and contains no code from either project.

## License

MIT — see [LICENSE](LICENSE).
