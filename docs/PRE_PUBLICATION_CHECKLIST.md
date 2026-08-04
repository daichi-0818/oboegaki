# Pre-publication checklist

Run every item before `gh repo create --public`. Publication requires an
explicit human go — a public push is cache-permanent even if deleted.

## Content audit
- [ ] `git ls-files` contains no caches/venv/binaries (`.gitignore` hardened)
- [ ] Secret scan over tracked files, **full git history**, untracked and
      ignored files, and build artifacts (`git log -p` grep or gitleaks)
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

## Quality gate
- [ ] `python3 -m unittest discover -s tests -v` — all green
- [ ] Zero-API tests pass (static + dynamic)
- [ ] Fault-injection suite passes (every check dimension calibrated)
- [ ] README quickstart commands re-executed verbatim on a fresh clone

## Publication (human-gated)
- [ ] Final human approval obtained immediately before `--public`
- [ ] `gh repo create --public` + topics
- [ ] CI workflow added; badge turns green on first run
