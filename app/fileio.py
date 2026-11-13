"""File access to a server's media: local (mounted into this container) or over ssh.

Jellyfin reports paths as *it* sees them (inside its container). Each server has path
maps that translate those into paths reachable here (local) or on the ssh host.
"""
import asyncio
import os
import posixpath
import shlex

from . import db

SSH_DIR = os.path.join(db.DATA_DIR, "ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
PARTIAL_DIR = ".jellymirror-partial"   # hidden: Jellyfin skips dot-folders while scanning
SIDECAR_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi"}


async def ensure_ssh_key():
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(SSH_KEY):
        p = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jellymirror", "-f", SSH_KEY)
        await p.wait()
        db.log("generated ssh key " + SSH_KEY)


def public_key():
    try:
        with open(SSH_KEY + ".pub") as f:
            return f.read().strip()
    except OSError:
        return ""


def to_fs(srv, jf_path):
    """Jellyfin path -> filesystem path (local here, or on the ssh host)."""
    best = None
    for jf_prefix, fs_prefix in srv["path_maps"]:
        jp = jf_prefix.rstrip("/")
        if jf_path == jp or jf_path.startswith(jp + "/"):
            if best is None or len(jp) > len(best[0]):
                best = (jp, fs_prefix.rstrip("/"))
    if best is None:
        return jf_path
    return best[1] + jf_path[len(best[0]):]


def ssh_base(srv):
    return ["ssh", "-i", SSH_KEY, "-p", str(srv.get("ssh_port") or 22),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "ServerAliveInterval=15",
            "-o", "ConnectTimeout=15"]


def ssh_target(srv):
    return f"{srv['ssh_user']}@{srv['ssh_host']}" if srv.get("ssh_user") else srv["ssh_host"]


def ssh_e(srv):
    """The -e argument for rsync."""
    return " ".join(shlex.quote(a) for a in ssh_base(srv))


async def run(args, stdin=None, check=True):
    p = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(stdin)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} exit {p.returncode}: {err.decode(errors='replace').strip()[:500]}")
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def sh(srv, script, check=True):
    """Run a shell snippet on the ssh host."""
    return await run(ssh_base(srv) + [ssh_target(srv), script], check=check)


class Files:
    """File operations on one server, by filesystem path (already mapped)."""

    def __init__(self, srv):
        self.srv = srv
        self.kind = srv["access"]
        if self.kind not in ("local", "ssh"):
            raise RuntimeError(f"server {srv['name']} has no file access (needs local or ssh)")

    async def size(self, path):
        """Size in bytes, or None if missing."""
        if self.kind == "local":
            try:
                return os.stat(path).st_size
            except FileNotFoundError:
                return None
        rc, out, _ = await sh(self.srv, f"stat -c %s {shlex.quote(path)} 2>/dev/null", check=False)
        return int(out.strip()) if rc == 0 and out.strip().isdigit() else None

    async def listdir(self, path):
        if self.kind == "local":
            try:
                return os.listdir(path)
            except FileNotFoundError:
                return []
        rc, out, _ = await sh(self.srv, f"ls -1A {shlex.quote(path)} 2>/dev/null", check=False)
        return [l for l in out.splitlines() if l] if rc == 0 else []

    async def mkdirs(self, path):
        if self.kind == "local":
            os.makedirs(path, exist_ok=True)
        else:
            await sh(self.srv, f"mkdir -p {shlex.quote(path)}")

    async def rename(self, src, dst):
        if self.kind == "local":
            os.replace(src, dst)
        else:
            await sh(self.srv, f"mv -f {shlex.quote(src)} {shlex.quote(dst)}")

    async def remove(self, path):
        if self.kind == "local":
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        else:
            await sh(self.srv, f"rm -f {shlex.quote(path)}")

    async def prune_dirs(self, path, stop_at):
        """Remove empty directories from path upwards, never at or above stop_at."""
        stop_at = stop_at.rstrip("/")
        cur = path.rstrip("/")
        dirs = []
        while cur.startswith(stop_at + "/"):
            dirs.append(cur)
            cur = posixpath.dirname(cur)
        if not dirs:
            return
        if self.kind == "local":
            for d in dirs:
                try:
                    pd = os.path.join(d, PARTIAL_DIR)
                    if os.path.isdir(pd) and not os.listdir(pd):
                        os.rmdir(pd)
                    os.rmdir(d)
                except OSError:
                    return
        else:
            script = " && ".join(
                f"{{ rmdir {shlex.quote(d + '/' + PARTIAL_DIR)} 2>/dev/null; rmdir {shlex.quote(d)} 2>/dev/null; }}"
                for d in dirs)
            await sh(self.srv, script, check=False)

    async def sidecars(self, video_path):
        """Subtitle files next to a video that share its name stem."""
        d, base = posixpath.split(video_path)
        stem = posixpath.splitext(base)[0]
        out = []
        for name in await self.listdir(d):
            if name != base and name.startswith(stem + ".") and posixpath.splitext(name)[1].lower() in SIDECAR_EXT:
                out.append(posixpath.join(d, name))
        return out
