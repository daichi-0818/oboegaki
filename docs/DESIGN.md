# Design

## Why files, not a database

A memory you cannot `grep`, `diff`, or code-review is a memory you cannot
trust. Markdown files in Git give you: history for free, sync for free,
conflict resolution you already know, and zero infrastructure. The cost is
that nothing enforces integrity — which is exactly what memkit adds.

## The human/automation boundary

Two artifacts, one direction of flow:

```
memory_spec.json   (human-authored: what sources back which context,
        │           which relations hold, which exceptions are accepted)
        ▼  refresh (deterministic)
memory_manifest.generated.json   (hashes, generated relations; do_not_edit)
        ▼  check
PASS / FAIL with exact reasons
```

Automation never writes into the human layer. Regeneration is always safe
because the manifest holds no hand-written content. This is the inverse of
tools that "helpfully" rewrite your notes: here, staleness surfaces as a
failing check, and a *person* decides how the context changes.

## Freshness as hashing, not similarity

An L2 context declares its L1 sources. `refresh` freezes each source's
SHA-256 (line-ending normalised). If a source changes afterwards, `check`
fails with "manifest is stale" until a human re-reads the changed source
and re-runs `refresh` — an explicit re-approval act. There is no "semantic
similarity" heuristic to quietly wave a change through.

## Typed relations instead of a knowledge graph service

Four verbs cover most memory hygiene:

- `supersedes` keeps history without letting it masquerade as current truth
- `contradicts` forces a decision: every contradiction must record a
  resolution whose winner is one of the endpoints
- `depends_on` documents fragile assumptions
- `derived_from` is generated, one per declared source, so provenance is
  queryable JSON rather than prose

## Orphans, duplicates, links

- A memory that no index references is unfindable — flagged as an orphan.
- Byte-identical duplicates (after newline normalisation) are flagged;
  intentional ones must be allow-listed as an exact group.
- Markdown links are checked as paths; `[[wiki links]]` are checked as
  logical memory IDs, matching hyphen/underscore variants and frontmatter
  `name:` declarations, so renames don't orphan references.
- Historical dead links must be listed one-by-one with a written reason —
  there is no blanket ignore.

## Symlink wiring

Agents that key memory directories off the working directory fragment
memory across paths. `link` declares "these live paths are views of this
canonical path" and enforces it: dry-run plan, timestamped backup of any
real directory, retarget of wrong symlinks, no-op when already correct.

## Scope decisions

- No embedding/LLM distillation: producing L2 is a judgment call; this
  tool verifies, it does not author.
- No daemon/watcher: run checks at session start or in CI.
- No auto-migration of legacy layouts: the spec is explicit configuration.
