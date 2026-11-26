"""
One way to write a data file, for every script on the desk.

WHY THIS EXISTS
---------------
funds.json alone had five writers, and they had drifted apart:

  * three wrote LF with a trailing newline (io.open(..., newline="\n")) and
    two used Path.write_text with none - so the file's last byte flipped
    between every weekly commit and the next daily one, a diff line that
    meant nothing.
  * Path.write_text translates "\n" to os.linesep, so run on Windows those
    two wrote CRLF. Only a global core.autocrlf kept that out of commits.
  * none of them was atomic. Opening for "w" truncates first, so a process
    killed mid-write - the workflow's step timeout does exactly that - left
    a half-written funds.json behind. CI's JSON check caught it; a local run
    had nothing to.

Here the bytes go to a temporary file beside the target and are swapped in
with os.replace, which is atomic on the same filesystem: a reader, or the
next step, sees the old file or the new one and never a torn one. Always
UTF-8, always LF.

Standard library only, like everything else here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def write_text(path, text: str) -> None:
    """Replace `path` with `text` atomically - UTF-8, LF, exactly as given."""
    path = Path(path)
    # mkstemp creates the file owner-only (0600). Left like that, a published
    # file would lose the read bits it had, so carry the old mode across, or
    # use ordinary 0644 for a file that did not exist yet.
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj) -> None:
    """The desk's one JSON layout: indent 2, real UTF-8, trailing newline."""
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Self-test - runs offline in a temporary directory
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    import sys as _sys

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "funds.json"

        write_json(p, {"name": "Café £", "n": [1, 2]})
        raw = p.read_bytes()
        assert raw.endswith(b"}\n"), raw[-5:]
        assert b"\r\n" not in raw, "must write LF on every platform"
        assert "Café £".encode() in raw, "must keep real UTF-8"
        assert json.loads(raw) == {"name": "Café £", "n": [1, 2]}

        write_text(p, "a\nb\n")
        assert p.read_bytes() == b"a\nb\n"
        print("  layout           OK  (UTF-8, LF, trailing newline)",
              file=_sys.stderr)

        # A write that fails halfway leaves the previous file exactly as it
        # was, and no temporary file behind.
        try:
            write_text(p, object())        # type: ignore[arg-type]
        except TypeError:
            pass
        else:
            raise AssertionError("a bad write must raise")
        assert p.read_bytes() == b"a\nb\n", "original must survive a failure"
        assert sorted(x.name for x in Path(d).iterdir()) == ["funds.json"], \
            list(Path(d).iterdir())
        print("  failed write     OK  (original intact, no temp left)",
              file=_sys.stderr)

        if os.name == "posix":
            os.chmod(p, 0o644)
            write_text(p, "x")
            assert stat.S_IMODE(p.stat().st_mode) == 0o644, \
                oct(p.stat().st_mode)
            fresh = Path(d) / "new.json"
            write_text(fresh, "y")
            assert stat.S_IMODE(fresh.stat().st_mode) == 0o644
            print("  permissions      OK  (0644 kept, not mkstemp's 0600)",
                  file=_sys.stderr)

    print("  datafile self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit("nothing to run - this is a library. Try --selftest.")
