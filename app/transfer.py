"""Job queue: copies and deletes, run with bounded parallelism, pausable and resumable.

Copies never write the final file name until the data is complete: rsync keeps its
partial file in a hidden .jellymirror-partial/ folder, and API downloads append to a
file there and are renamed when complete. Pausing stops the transfer; resuming picks the
partial file up again (rsync uses it as a delta basis, API downloads use a Range request).
"""
import asyncio
import os
import posixpath
import re
import shutil
import time

import httpx

from . import db
import shlex

from .fileio import PARTIAL_DIR, Files, ssh_base, ssh_e, ssh_target, to_fs
from .jellyfin import Jellyfin

STAGING = os.environ.get("JM_STAGING", os.path.join(db.DATA_DIR, "staging"))
PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d+)%")

_tasks: dict[int, asyncio.Task] = {}
_refresh: dict[int, set] = {}        # target_id -> {(jellyfin_path, update_type)}


# --- job bookkeeping ------------------------------------------------------------------
def enqueue(target_id, kind, dest_path, src_path=None, source_item_id=None, size=None, method=None):
    dup = db.one("SELECT id FROM jobs WHERE target_id=? AND dest_path=? AND kind=? "
                 "AND status IN ('queued','running','paused')", (target_id, dest_path, kind))
    if dup:
        return dup["id"]
    now = time.time()
    return db.x("INSERT INTO jobs(target_id, kind, source_item_id, src_path, dest_path, size, method, "
                "created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                (target_id, kind, source_item_id, src_path, dest_path, size, method, now, now))


def set_status(job_id, status, error=None):
    db.x("UPDATE jobs SET status=?, error=?, updated=? WHERE id=?", (status, error, time.time(), job_id))


def pause(job_id):
    set_status(job_id, "paused")
    t = _tasks.get(job_id)
    if t:
        t.cancel()


def resume(job_id):
    db.x("UPDATE jobs SET status='queued', error=NULL, updated=? WHERE id=? AND status IN "
         "('paused','failed','cancelled')", (time.time(), job_id))


def cancel(job_id):
    set_status(job_id, "cancelled")
    t = _tasks.get(job_id)
    if t:
        t.cancel()


# --- worker ---------------------------------------------------------------------------
async def worker():
    while True:
        try:
            limit = db.setting("max_parallel_jobs", int)
            running = sum(1 for t in _tasks.values() if not t.done())
            if running < limit:
                for job in db.q("SELECT * FROM jobs WHERE status='queued' ORDER BY kind='copy', id LIMIT ?",
                                (limit - running,)):
                    if job["id"] in _tasks and not _tasks[job["id"]].done():
                        continue
                    set_status(job["id"], "running")
                    _tasks[job["id"]] = asyncio.create_task(_run_job(job["id"]))
            for jid in [j for j, t in _tasks.items() if t.done()]:
                del _tasks[jid]
            await _flush_refreshes()
        except Exception as e:  # keep the worker alive
            db.log(f"worker: {e!r}", "error")
        await asyncio.sleep(2)


async def _run_job(job_id):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    tgt = db.one("SELECT * FROM targets WHERE id=?", (job["target_id"],))
    rule = db.one("SELECT * FROM rules WHERE id=?", (tgt["rule_id"],))
    src, dst = db.server(rule["source_server_id"]), db.server(tgt["server_id"])
    db.x("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job_id,))
    try:
        if job["kind"] == "delete":
            await _delete(job, tgt, dst)
        else:
            await _copy(job, tgt, src, dst)
        set_status(job_id, "done")
    except asyncio.CancelledError:
        # paused/cancelled by the user: the status is already set; partial data is kept
        pass
    except Exception as e:
        db.log(f"job {job_id} {job['kind']} {job['dest_path']}: {e}", "error")
        set_status(job_id, "failed", str(e)[:1000])


def _progress(job_id, done):
    db.x("UPDATE jobs SET progress=?, updated=? WHERE id=?", (done, time.time(), job_id))


# --- delete ---------------------------------------------------------------------------
async def _delete(job, tgt, dst):
    placed = db.one("SELECT * FROM placed WHERE target_id=? AND dest_path=?", (tgt["id"], job["dest_path"]))
    if not placed or not placed["owned"]:
        raise RuntimeError("refusing to delete a file jellymirror did not copy")
    files = Files(dst)
    fs = to_fs(dst, job["dest_path"])
    await files.remove(fs)
    await files.prune_dirs(posixpath.dirname(fs), to_fs(dst, tgt["root"]))
    db.x("DELETE FROM placed WHERE id=?", (placed["id"],))
    db.log(f"deleted {dst['name']}:{job['dest_path']}")
    _refresh.setdefault(tgt["id"], set()).add((job["dest_path"], "Deleted"))


# --- copy -----------------------------------------------------------------------------
def resolve_method(method, src, dst):
    if dst["access"] not in ("local", "ssh"):
        raise RuntimeError(f"target {dst['name']} needs local or ssh file access to receive files")
    if method == "auto":
        return "rsync" if src["access"] in ("local", "ssh") else "api"
    if method == "rsync" and src["access"] not in ("local", "ssh"):
        raise RuntimeError(f"rsync needs file access to the source {src['name']}")
    return method


async def _copy(job, tgt, src, dst):
    method = resolve_method(job["method"] or tgt["method"], src, dst)
    db.x("UPDATE jobs SET method=? WHERE id=?", (method, job["id"]))
    dst_files = Files(dst)
    dest_fs = to_fs(dst, job["dest_path"])
    if src["access"] in ("local", "ssh") and job["size"]:
        now = await Files(src).size(to_fs(src, job["src_path"]))
        if now != job["size"]:
            raise RuntimeError(f"source changed since it was planned ({job['size']} -> {now} bytes); "
                               "the next sync plans it again")
    await dst_files.mkdirs(posixpath.dirname(dest_fs))
    if method == "rsync":
        await _rsync_copy(job, tgt, src, dst, to_fs(src, job["src_path"]), dest_fs)
    else:
        await _api_copy(job, tgt, src, dst_files, dest_fs)
    final = await dst_files.size(dest_fs)
    if final is None or (job["size"] and final != job["size"]):
        raise RuntimeError(f"size check failed: expected {job['size']}, found {final}")
    db.x("INSERT INTO placed(target_id, dest_path, source_path, source_size, owned, created) "
         "VALUES (?,?,?,?,1,?) ON CONFLICT(target_id, dest_path) DO UPDATE SET "
         "source_path=excluded.source_path, source_size=excluded.source_size",
         (tgt["id"], job["dest_path"], job["src_path"], job["size"], time.time()))
    _progress(job["id"], final)
    db.log(f"copied {src['name']}:{job['src_path']} -> {dst['name']}:{job['dest_path']} ({method})")
    _refresh.setdefault(tgt["id"], set()).add((posixpath.dirname(job["dest_path"]), "Created"))


def _rsync_args(tgt):
    args = ["rsync", "-t", "-s", "--partial", f"--partial-dir={PARTIAL_DIR}",
            "--info=progress2", "--no-inc-recursive"]
    if tgt["bwlimit_kbps"]:
        args.append(f"--bwlimit={tgt['bwlimit_kbps']}")
    return args


def _rsync_loc(srv, path):
    return f"{ssh_target(srv)}:{path}" if srv["access"] == "ssh" else path


async def _rsync(job_id, args, offset=0):
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        buf, last = b"", 0.0
        while True:
            chunk = await p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for line in lines:
                m = PROGRESS_RE.match(line.decode(errors="replace"))
                if m and time.time() - last > 1:
                    last = time.time()
                    _progress(job_id, offset + int(m.group(1).replace(",", "")))
        err = (await p.stderr.read()).decode(errors="replace").strip()
        if await p.wait() != 0:
            raise RuntimeError(f"rsync exit {p.returncode}: {err[-500:]}")
    except asyncio.CancelledError:
        if p.returncode is None:
            p.terminate()   # rsync keeps the partial file on SIGTERM
            try:
                await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError:
                p.kill()
        raise


async def _rsync_copy(job, tgt, src, dst, src_fs, dest_fs):
    base = _rsync_args(tgt)
    if src["access"] == "ssh" and dst["access"] == "ssh":
        # rsync cannot copy between two remotes: relay through local staging (both legs resumable)
        stage = os.path.join(STAGING, str(job["id"]))
        os.makedirs(stage, exist_ok=True)
        local = os.path.join(stage, posixpath.basename(src_fs))
        await _rsync(job["id"], base + ["-e", ssh_e(src), _rsync_loc(src, src_fs), local])
        await _rsync(job["id"], base + ["-e", ssh_e(dst), local, _rsync_loc(dst, dest_fs)])
        shutil.rmtree(stage, ignore_errors=True)
        return
    remote = src if src["access"] == "ssh" else dst if dst["access"] == "ssh" else None
    args = base + (["-e", ssh_e(remote)] if remote else [])
    await _rsync(job["id"], args + [_rsync_loc(src, src_fs), _rsync_loc(dst, dest_fs)])


async def _api_copy(job, tgt, src, dst_files, dest_fs):
    """Download through the source's Jellyfin API, appending to a partial file (resumable)."""
    d, name = posixpath.split(dest_fs)
    part_dir = posixpath.join(d, PARTIAL_DIR)
    part = posixpath.join(part_dir, name)
    await dst_files.mkdirs(part_dir)
    offset = await dst_files.size(part) or 0
    if not (job["size"] and offset == job["size"]):
        jf = Jellyfin(src)
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with jf.client(timeout=httpx.Timeout(60, read=300)) as c:
            async with c.stream("GET", f"/Items/{job['source_item_id']}/Download", headers=headers) as r:
                if r.status_code == 416:
                    offset = 0                # partial is bigger than the file: start over
                    await r.aclose()
                    await dst_files.remove(part)
                    raise RuntimeError("partial file did not fit the source; restarting on the next try")
                r.raise_for_status()
                if offset:
                    m = re.match(r"bytes (\d+)-", r.headers.get("content-range", ""))
                    if r.status_code != 206 or not m or int(m.group(1)) != offset:
                        offset = 0            # range ignored or answered from elsewhere: start over
                await _write_stream(job, tgt, dst_files, part, r, offset)
    await dst_files.rename(part, dest_fs)
    await dst_files.prune_dirs(part_dir, posixpath.dirname(d))


async def _write_stream(job, tgt, files, part, resp, offset):
    limit = (tgt["bwlimit_kbps"] or 0) * 1024
    done, started, last = offset, time.time(), 0.0
    if files.kind == "local":
        f = open(part, "ab" if offset else "wb")
        write = lambda b: f.write(b)
        close = f.close
        proc = None
    else:
        cmd = f"cat {'>>' if offset else '>'} {shlex.quote(part)}"
        proc = await asyncio.create_subprocess_exec(*ssh_base(files.srv), ssh_target(files.srv), cmd,
                                                    stdin=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        write = proc.stdin.write
        close = proc.stdin.close
    try:
        async for chunk in resp.aiter_bytes(1 << 20):
            write(chunk)
            if proc:
                await proc.stdin.drain()
            done += len(chunk)
            if time.time() - last > 1:
                last = time.time()
                _progress(job["id"], done)
            if limit:
                ahead = (done - offset) / limit - (time.time() - started)
                if ahead > 0:
                    await asyncio.sleep(ahead)
        _progress(job["id"], done)
    finally:
        close()
        if proc:
            err = (await proc.stderr.read()).decode(errors="replace")
            if await proc.wait() != 0:
                raise RuntimeError(f"ssh write failed: {err.strip()[:300]}")


# --- library refresh on targets -------------------------------------------------------
async def _flush_refreshes():
    for target_id in list(_refresh):
        busy = db.one("SELECT 1 FROM jobs WHERE target_id=? AND status IN ('queued','running')", (target_id,))
        if busy:
            continue
        updates = _refresh.pop(target_id)
        tgt = db.one("SELECT * FROM targets WHERE id=?", (target_id,))
        if not tgt:
            continue
        srv = db.server(tgt["server_id"])
        try:
            if not tgt["matched_item_id"] or len(updates) > 250:
                # Jellyfin drops a per-path report for a folder it does not know yet
                await Jellyfin(srv).refresh_library()
                db.log(f"asked {srv['name']} for a library scan (new title)")
            else:
                await Jellyfin(srv).media_updated(sorted(updates))
                db.log(f"asked {srv['name']} to scan {len(updates)} path(s)")
        except Exception as e:
            db.log(f"scan request to {srv['name']} failed: {e}", "warn")
