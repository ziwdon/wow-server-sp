#!/usr/bin/env python3
"""Stand-in for the ``proton-drive`` binary, driven by ``FAKE_CLI_*`` variables.

It records what the wrapper asked for, then answers with canned output:

* ``FAKE_CLI_ARGV_LOG``  file that gets one JSON line per run: the argv, the
  whole environment it was given, the three variables the real CLI reads, and
  whether stdin was ``/dev/null``
* ``FAKE_CLI_STDOUT``    printed to stdout
* ``FAKE_CLI_STDERR``    printed to stderr
* ``FAKE_CLI_EXIT``      exit status (default 0)

The tests copy it into ``tmp_path`` with the right shebang and mark it
executable; nothing here ever talks to Proton.
"""

import json
import os
import sys


def _stdin_is_devnull() -> bool:
    try:
        return os.path.samestat(os.fstat(0), os.stat(os.devnull))
    except OSError:
        return False


def main() -> int:
    log_path = os.environ.get("FAKE_CLI_ARGV_LOG")
    if log_path:
        row = {
            "argv": sys.argv[1:],
            "env": dict(os.environ),
            "PROTON_DRIVE_CREDENTIALS_STORE": os.environ.get("PROTON_DRIVE_CREDENTIALS_STORE"),
            "PROTON_DRIVE_CACHE_DIR": os.environ.get("PROTON_DRIVE_CACHE_DIR"),
            "GNUPGHOME": os.environ.get("GNUPGHOME"),
            "stdin_is_devnull": _stdin_is_devnull(),
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    sys.stdout.write(os.environ.get("FAKE_CLI_STDOUT", ""))
    sys.stdout.flush()
    sys.stderr.write(os.environ.get("FAKE_CLI_STDERR", ""))
    sys.stderr.flush()
    return int(os.environ.get("FAKE_CLI_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
