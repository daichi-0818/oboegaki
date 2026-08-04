# Security Policy

## Design guarantees

- **No network access.** memkit performs zero API calls, opens no sockets,
  and spawns no subprocesses. This is enforced by `tests/test_zero_api.py`
  (static import scan + dynamic socket bomb).
- **No data leaves your machine.** All hashing and checking is local.
- **No destructive file operations.** `link` moves existing real
  directories to timestamped backups; nothing is deleted. The only file
  memkit writes is the generated manifest (atomic replace).
- **Path containment.** Workspace-relative paths in the spec may not
  escape the workspace root.

## Threat model notes

- Memory files are treated as *data*. memkit never executes content found
  in Markdown or JSON.
- The generated manifest is reproducible from the spec; tampering is
  detected by `check` (see the fault-injection tests).
- memkit does not manage secrets. Do not store credentials in memory
  files; pair this tool with a secret scanner (e.g. gitleaks) in CI if
  your memory workspace is git-hosted.

## Reporting a vulnerability

Open a GitHub security advisory or an issue with the `security` label.
Please include a minimal reproduction. Because the tool is a single
stdlib-only file with no runtime dependencies, most reports can be
triaged quickly.
