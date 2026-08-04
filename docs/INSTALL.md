# Install

memkit is a single Python file with zero dependencies. "Install" means:
have Python 3.9+, copy or clone, run.

## macOS

```bash
python3 --version              # 3.9+ (ships with Xcode CLT / homebrew)
git clone <this-repo> && cd oboegaki
python3 memkit.py check --workspace samples/demo
```

## Linux (Debian/Ubuntu)

```bash
sudo apt-get install -y python3 git   # if missing
git clone <this-repo> && cd oboegaki
python3 memkit.py check --workspace samples/demo
```

## Windows (WSL2)

Run inside your WSL distribution (Ubuntu recommended). Symlinks created by
`link` are native Linux symlinks inside the WSL filesystem.

```bash
sudo apt-get install -y python3 git
git clone <this-repo> && cd oboegaki
python3 memkit.py check --workspace samples/demo
```

Notes for WSL:
- Keep the memory workspace on the Linux side (`~/...`), not under
  `/mnt/c/...`, for correct symlink and permission semantics.
- CRLF line endings introduced by Windows editors do not cause false
  staleness — hashing normalises line endings.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

All suites are stdlib-only; no pip install is required.

## Optional: put it on your PATH

```bash
chmod +x memkit.py
ln -s "$PWD/memkit.py" ~/.local/bin/memkit
memkit check --workspace ~/my-memory
```
