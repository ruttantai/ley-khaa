#!/usr/bin/env python3
"""Start the backend as an unprivileged user, after doing the two things only
root can do inside this container.

There is no `USER app` line in the Dockerfile, and that is deliberate. The
backend spawns each sandbox with `--user <our uid>:<our gid>`, so the backend's
own uid IS the sandbox's isolation boundary — a root backend produces a root
sandbox, which is what spec §4.1 forbids and what README and CHANGELOG say does
not happen. Two things stop a build-time `USER` from getting us there:

  * the compose backend talks to the host's docker socket, which is mode 0660
    owned by a group whose gid differs per host (991 under Colima, 999 on a
    stock Debian daemon, 0 under Docker Desktop) and so cannot be baked into an
    image; without membership of that group DockerSandbox.available() is False
    and every run silently downgrades to the weaker fallback; and
  * task-workspaces is a named volume, which an earlier `compose up` may have
    left owned by root.

Both are fixable at start-up, and only by root. So: start as root, join whatever
group owns the socket, hand the workspace to the app user, drop, and exec.
uvicorn — and therefore every sandbox container it spawns — is unprivileged from
that point on.
"""
from __future__ import annotations

import os
import pwd
import sys

_APP_USER = "app"
_DOCKER_SOCKET = "/var/run/docker.sock"
# Same default as ley_khaa.config.Settings, which this cannot import: the
# entrypoint runs before the app and must not depend on it being importable.
_WORKSPACE_ROOT = os.getenv("LEY_KHAA_WORKSPACE_ROOT", "./task-workspaces")


def _chown_tree(path: str, uid: int, gid: int) -> None:
    """Give the whole workspace to the app user, without following links.

    A bundle's contents are written by synthesized code, so a symlink in there
    is something to expect rather than to trust: chowning through one would let
    a script hand itself a file outside the volume.
    """
    os.chown(path, uid, gid, follow_symlinks=False)
    for parent, directories, files in os.walk(path):  # os.walk does not follow links
        for name in directories + files:
            os.chown(os.path.join(parent, name), uid, gid, follow_symlinks=False)


def main(command: list[str]) -> None:
    if not command:
        raise SystemExit("nothing to run: pass the command as arguments")
    if os.getuid() != 0:
        os.execvp(command[0], command)  # already unprivileged; nothing to arrange

    account = pwd.getpwnam(_APP_USER)
    groups = [account.pw_gid]
    try:
        socket_gid = os.stat(_DOCKER_SOCKET).st_gid
    except OSError:
        # No socket mounted. DockerSandbox.available() will say so and the
        # fallback announces itself; that is a louder failure than running as
        # root to keep a socket we may not even have.
        socket_gid = None
    if socket_gid is not None and socket_gid not in groups:
        groups.append(socket_gid)

    os.makedirs(_WORKSPACE_ROOT, exist_ok=True)
    _chown_tree(_WORKSPACE_ROOT, account.pw_uid, account.pw_gid)

    os.setgroups(groups)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)  # last: nothing above is possible after this
    os.environ["HOME"] = account.pw_dir
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
