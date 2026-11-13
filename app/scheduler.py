"""Background loops: library change polling, due-target runner, periodic full reconcile."""
import asyncio
import hashlib
import time

from . import db, sync, watch


def _set_status(sid, status):
    db.x("UPDATE servers SET status=?, status_at=? WHERE id=?", (status, time.time(), sid))


async def poll_libraries():
    """Fingerprint each title's files on its source; a change marks the rule's targets due.

    (minDateLastSaved would be cheaper, but Jellyfin 10.11 ignores it.)
    """
    status = {}
    for r in db.q("SELECT r.* FROM rules r WHERE r.enabled=1 AND EXISTS "
                  "(SELECT 1 FROM targets t WHERE t.rule_id=r.id AND t.sync_files=1)"):
        src = db.server(r["source_server_id"])
        if not src or not src["enabled"]:
            continue
        try:
            files, _ = await sync.source_files(r, src)
            status[src["id"]] = "ok"
        except Exception as e:
            status[src["id"]] = f"unreachable: {e}"
            continue
        if files is None:
            continue
        sig = hashlib.sha1("\n".join(sorted(f"{p}|{s}" for _, _, p, s in files)).encode()).hexdigest()
        if sig != r["src_sig"]:
            db.x("UPDATE rules SET src_sig=? WHERE id=?", (sig, r["id"]))
            if r["src_sig"] is not None:
                for t in db.q("SELECT id FROM targets WHERE rule_id=? AND sync_files=1", (r["id"],)):
                    sync.mark_due(t["id"])
                db.log(f"files changed for '{r['title']}' on {src['name']}")
    for sid, st in status.items():
        _set_status(sid, st)


async def run_due():
    now = time.time()
    for t in db.q("SELECT t.id FROM targets t JOIN rules r ON r.id=t.rule_id "
                  "WHERE r.enabled=1 AND t.due_at IS NOT NULL AND t.due_at<=?", (now,)):
        await sync.apply(t["id"])


def reconcile_all():
    for t in db.q("SELECT t.id FROM targets t JOIN rules r ON r.id=t.rule_id WHERE r.enabled=1"):
        sync.mark_due(t["id"], now=True)
    watch.reconcile_requested.set()


async def _loop(fn, interval_key, default=60):
    while True:
        try:
            await fn()
        except Exception as e:
            db.log(f"{fn.__name__}: {e!r}", "error")
        try:
            interval = db.setting(interval_key, int) if interval_key else default
        except Exception:
            interval = default
        await asyncio.sleep(max(5, interval))


async def _reconcile_loop():
    while True:
        hours = db.setting("reconcile_hours", float)
        last = float(db.setting("last_reconcile") or 0) if db.one(
            "SELECT 1 FROM settings WHERE key='last_reconcile'") else 0
        if time.time() - last >= hours * 3600:
            db.set_setting("last_reconcile", time.time())
            db.log("periodic reconcile of all rules")
            reconcile_all()
        await asyncio.sleep(60)


def start():
    from . import transfer
    return [
        asyncio.create_task(transfer.worker()),
        asyncio.create_task(_loop(poll_libraries, "lib_poll_seconds")),
        asyncio.create_task(_loop(run_due, None, 10)),
        asyncio.create_task(_loop(watch.poll, "watch_poll_seconds")),
        asyncio.create_task(watch.reconcile_loop()),
        asyncio.create_task(_reconcile_loop()),
    ]
