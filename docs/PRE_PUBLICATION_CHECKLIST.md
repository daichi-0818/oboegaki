# Pre-publication checklist

Publication requires an explicit human go — a public push is
cache-permanent even if deleted. The flow is staged: everything below is
verified **before** the repo ever becomes public, using a private staging
repo for the clone-based checks (a fresh clone of the published URL can,
by definition, only happen after publication — so it runs against the
private remote first, and once more as a post-publication smoke test).

## Content audit
- [ ] `git ls-files` contains no caches/venv/binaries (`.gitignore` hardened)
- [ ] **Run a dedicated secret scanner (gitleaks or equivalent) immediately
      before publication** over the working tree AND full git history —
      pattern greps during development do not replace this step
- [ ] Secret grep over tracked files, untracked and ignored files, and
      build artifacts (belt-and-suspenders alongside the scanner)
- [ ] Binaries (if any ever appear) inspected with `strings`
- [ ] No real personal names, emails, employers, hostnames, usernames, or
      absolute private paths anywhere (grep for your own identifiers)
- [ ] Sample data is entirely fictional; placeholder domains only
      (`example.com`)

## Rights audit
- [ ] All code is original or license-compatible; third-party code, fonts,
      images, and sample datasets have redistribution rights
- [ ] Idea-level inspirations acknowledged in README with their licenses
- [ ] LICENSE file present and copyright line intentional

## Quality gate (local)
- [ ] `python3 -m unittest discover -s tests -v` — all green
- [ ] Zero-API tests pass (static + dynamic)
- [ ] Fault-injection suite passes (every check dimension calibrated)
- [ ] README quickstart local steps re-executed on a fresh copy

## Publication order (human-gated, private staging first)
1. [ ] Human decisions fixed: commit author identity / repository name /
       LICENSE copyright line / design-lineage publication rights
2. [ ] gitleaks (or equivalent) run locally over the working tree AND the
       full git history — zero findings
3. [ ] Explicit approval to stage → `gh repo create --private` + push
4. [ ] Fresh clone **from the private remote**; run the full test suite
       and the README quickstart verbatim from that clone
5. [ ] Final human approval to publish
6. [ ] Repository visibility flipped to public + topics set
7. [ ] Post-publication smoke test: fresh clone from the **public** URL,
       quickstart re-run; CI workflow added and badge green
