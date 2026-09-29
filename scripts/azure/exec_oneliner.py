"""Wrap a Python file into ONE `python -c <code>` command for `az containerapp exec`.

    python3 scripts/azure/exec_oneliner.py scripts/azure/diag_keys.py [--for split|shell]

`az containerapp exec --command` takes a single string, and piping a script to `python -`
needs a working stdin through the exec websocket. The T11.1.3 spike tells which of these
works on the platform:

- stdin works → `python -` with the file on stdin; this tool is not needed.
- `--command "python -c print(12345)"` prints 12345 → the string is SPLIT ON WHITESPACE
  (no shell): use `--for split` (default) — the code argument has no whitespace, so it
  stays one argument, and no quotes are needed around it.
- it fails with a shell syntax error → a SHELL parses the string: use `--for shell`, which
  wraps the code in double quotes (the code contains no `"`, `$`, backtick or backslash).

The code is the file, zlib-compressed and base64-encoded:
`exec(__import__('zlib').decompress(__import__('base64').b64decode('…')))`.
Prints the command on stdout; nothing is executed here.
"""

from __future__ import annotations

import argparse
import base64
import zlib
from pathlib import Path


def oneliner(source: str, target: str = "split") -> str:
    b64 = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    code = "exec(__import__('zlib').decompress(__import__('base64').b64decode('" + b64 + "')))"
    assert not any(c.isspace() or c in '$`\\"' for c in code)
    if target == "split":
        return f"python -c {code}"
    if target == "shell":
        return f'python -c "{code}"'
    raise ValueError(f"unknown target {target!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path)
    parser.add_argument("--for", dest="target", choices=("split", "shell"), default="split")
    args = parser.parse_args()
    print(oneliner(args.file.read_text(encoding="utf-8"), args.target))


if __name__ == "__main__":
    main()
