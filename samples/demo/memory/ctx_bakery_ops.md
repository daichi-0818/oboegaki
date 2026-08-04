---
name: ctx-bakery-ops
description: "L2 context: current truth for Orbital Bakery operations"
---

# Bakery Ops — Current Context (L2)

> Distilled "what is true now". History lives in the linked L1 files.
> Verify freshness before relying on this: `python3 memkit.py check`.

## Rules that must never be broken
- The oven is never left unattended while heating — see
  [[feedback-never-leave-oven-unattended]] (note: wiki links match both
  hyphen and underscore file names).
- Recipes are scaled **by weight**, not volume
  ([feedback](feedback_scale_recipes_by_weight.md)). The old volume-based
  rule is archived and lost the contradiction.

## Current state
- Oven controller **v2** is live (PID, 2026-01-10). v1 is superseded —
  see [v2 notes](project_oven_controller_v2.md).
- Sourdough tracker depends on the
  [flour supplier API](reference_flour_supplier_api.md) for reorder dates.

## Generated section note
The provenance (source paths + SHA-256) for this context is materialised in
`generated/memory_manifest.json` by `memkit.py refresh`. Humans edit this
file; the manifest is never edited by hand.
