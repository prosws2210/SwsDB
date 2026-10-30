"""Holds an exclusive lock on the data volume for as long as the container runs.

Two swsdb containers on one volume would run two Postgres servers on the same files and corrupt them. This process
starts before anything else, takes `flock` on a file in the volume and keeps it; a second container's guard can't, and
stops that container with a clear message. Holding the lock is also what makes it safe for bootstrap to clear a
`postmaster.pid` left behind by a container that was killed.
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .settings import Settings


def main() -> None:
    s = Settings()
    lock_path = s.data_dir / ".swsdb.lock"
    ok_flag = s.run_dir / "guard.ok"
    s.run_dir.mkdir(parents=True, exist_ok=True)
    ok_flag.unlink(missing_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[swsdb] fatal: the data volume ({s.data_dir}) is already used by another running swsdb container. "
              "Stop that container first, or give this one its own volume.", file=sys.stderr, flush=True)
        _halt()
        return
    except OSError as e:
        if e.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        # some network or host-mounted filesystems can't lock; run, but say what isn't protected
        print(f"[swsdb] WARNING: the data volume's filesystem doesn't support locks ({e.strerror}); swsdb can't "
              "stop two containers from using it at once. Use a Docker volume for production.", flush=True)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode())
    Path(ok_flag).write_text("ok\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:  # hold the lock until the container stops
        signal.pause()


def _halt() -> None:
    # stop the whole container: nothing may start on a volume another container owns
    subprocess.run(["/run/s6/basedir/bin/halt"], check=False)
    time.sleep(30)


if __name__ == "__main__":
    main()
