"""Per-user, per-title watch state sync between every server in a rule.

Items are matched by title (series/movie id on each server, found by provider ids) plus
season/episode number, so paths and libraries do not need to agree.

Change detection: every poll reads each person's state of each watch-synced title on each
server in the rule (one request per title, person and server). Every observed and every
written state is kept as a snapshot per (server, user, item); an observation equal to the
snapshot is not a change. That is also the echo guard: what we write is stored first, so
reading it back is a no-op.

Rules borrowed from WatchState / watch-state-sync / JellySync:
- the other side is re-read right before writing; no write if it already matches
- an item someone is playing right now is not written
- positions under MIN_PROGRESS are not propagated
- played -> unplayed with a resume position is a rewatch, not an "unwatch": only the
  position travels; played -> unplayed at position 0 is a deliberate unmark and travels
- PlayCount never goes down; LastPlayedDate travels with the change, never "now"
- the initial / periodic merge: newest LastPlayedDate wins; with no dates, played wins
"""
import asyncio
import json
import time

from . import db
from .jellyfin import Jellyfin

MIN_PROGRESS = 60 * 10_000_000      # 60 s in ticks

reconcile_requested = asyncio.Event()
_rule_requests: set[int] = set()


def request_rule(rule_id):
    _rule_requests.add(rule_id)
    reconcile_requested.set()


# --- helpers ---------------------------------------------------------------------------
def _key(item):
    if item.get("Type") == "Episode":
        return (item.get("ParentIndexNumber"), item.get("IndexNumber"))
    return ("movie",)


def _norm(ud):
    pos = ud.get("PlaybackPositionTicks") or 0
    return {"played": bool(ud.get("Played")), "pos": int(pos // 10_000_000) * 10_000_000,
            "fav": bool(ud.get("IsFavorite"))}


def _snap_get(sid, uid, iid):
    r = db.one("SELECT state FROM applied WHERE server_id=? AND user_id=? AND item_id=?", (sid, uid, iid))
    return json.loads(r["state"]) if r else None


def _snap_put(sid, uid, iid, state):
    db.x("INSERT INTO applied(server_id, user_id, item_id, state, at) VALUES (?,?,?,?,?) "
         "ON CONFLICT(server_id, user_id, item_id) DO UPDATE SET state=excluded.state, at=excluded.at",
         (sid, uid, iid, json.dumps(state, sort_keys=True), time.time()))


def _title_index():
    """(server_id, title item id) -> rule, for rules with watch sync on."""
    idx = {}
    for r in db.q("SELECT * FROM rules WHERE enabled=1 AND sync_watch=1"):
        idx[(r["source_server_id"], r["source_item_id"])] = r
        for t in db.q("SELECT server_id, matched_item_id FROM targets WHERE rule_id=? AND matched_item_id "
                      "IS NOT NULL", (r["id"],)):
            idx[(t["server_id"], t["matched_item_id"])] = r
    return idx


def _rule_servers(rule):
    """[(server_id, title item id)] for every server in the rule where the title is known."""
    out = [(rule["source_server_id"], rule["source_item_id"])]
    out += [(t["server_id"], t["matched_item_id"]) for t in
            db.q("SELECT server_id, matched_item_id FROM targets WHERE rule_id=? AND matched_item_id IS NOT NULL",
                 (rule["id"],))]
    return out


def _persons_for(rule):
    chosen = [r["person_id"] for r in db.q("SELECT person_id FROM rule_users WHERE rule_id=?", (rule["id"],))]
    return set(chosen) if chosen else {p["id"] for p in db.q("SELECT id FROM persons")}


def _account(person_id, server_id):
    return db.one("SELECT * FROM accounts WHERE person_id=? AND server_id=?", (person_id, server_id))


async def _playing(srv):
    """{(user_id, item_id)} being played right now on a server."""
    try:
        sessions = await Jellyfin(srv).get("/Sessions", activeWithinSeconds=300)
    except Exception:
        return set()
    return {(s.get("UserId"), (s.get("NowPlayingItem") or {}).get("Id")) for s in sessions or []
            if s.get("NowPlayingItem")}


async def _title_items(srv, user_id, title_id):
    return {_key(i): i for i in await Jellyfin(srv).title_user_items(user_id, title_id)}


async def _write(srv, user_id, item, new, last_played, playing, reason):
    """Apply a normalized state to one item, unless it already matches or is playing."""
    if (user_id, item["Id"]) in playing:
        db.log(f"watch: skipped {srv['name']} {item.get('SeriesName') or ''} {item['Name']}: playing now")
        return False
    cur_raw = await Jellyfin(srv).user_data(user_id, item["Id"])
    cur = _norm(cur_raw)
    if cur == new:
        _snap_put(srv["id"], user_id, item["Id"], cur)
        return False
    body = {"Played": new["played"], "PlaybackPositionTicks": 0 if new["played"] else new["pos"],
            "IsFavorite": new["fav"]}
    if new["played"]:
        body["PlayCount"] = max(cur_raw.get("PlayCount") or 0, 1)
    if last_played:
        body["LastPlayedDate"] = last_played
    _snap_put(srv["id"], user_id, item["Id"], new)     # before writing: the echo is then a no-op
    await Jellyfin(srv).set_user_data(user_id, item["Id"], body)
    after = _norm(await Jellyfin(srv).user_data(user_id, item["Id"]))
    if after != new:
        _snap_put(srv["id"], user_id, item["Id"], after)
        db.log(f"watch: {srv['name']} {item['Name']}: wrote {new}, server kept {after}", "warn")
    label = f"{item.get('SeriesName') + ' ' if item.get('SeriesName') else ''}{item['Name']}"
    db.log(f"watch: {srv['name']} {label}: {reason}")
    return True


# --- incremental poll ------------------------------------------------------------------
async def poll():
    """Read every person's state of every watch-synced title on every server in its rule,
    diff it against the stored snapshots, and carry real changes to the other servers.

    (minDateLastSavedForUser looks like the cheap way to do this, but on 10.11 it filters
    on when the *item* was last saved by a scan, not when the user's data changed.)
    """
    playing = {}
    written = set()     # (server id, item id) written this poll: their views are stale
    for rule in db.q("SELECT * FROM rules WHERE enabled=1 AND sync_watch=1"):
        servers = _rule_servers(rule)
        if len(servers) < 2:
            continue
        for pid in _persons_for(rule):
            views = []
            for sid, title_id in servers:
                acc, srv = _account(pid, sid), db.server(sid)
                if not acc or not srv or not srv["enabled"]:
                    continue
                try:
                    views.append((srv, acc, await _title_items(srv, acc["jf_user_id"], title_id)))
                except Exception:
                    continue    # unreachable: its changes are picked up on a later poll
            if len(views) < 2:
                continue
            for srv, acc, items in views:
                for key, item in items.items():
                    if (srv["id"], item["Id"]) in written:
                        continue
                    ud = item.get("UserData") or {}
                    new = _norm(ud)
                    old = _snap_get(srv["id"], acc["jf_user_id"], item["Id"])
                    if old == new:
                        continue
                    if srv["id"] not in playing:
                        playing[srv["id"]] = await _playing(srv)
                    if (acc["jf_user_id"], item["Id"]) in playing[srv["id"]]:
                        continue    # still playing: picked up once playback stops
                    _snap_put(srv["id"], acc["jf_user_id"], item["Id"], new)
                    await _propagate(views, srv, acc, key, old, new, ud.get("LastPlayedDate"), playing, written)


async def _propagate(views, src_srv, acc, key, old, new, last_played, playing_cache, written):
    person = db.one("SELECT name FROM persons WHERE id=?", (acc["person_id"],))["name"]
    for srv, other, items in views:
        sid = srv["id"]
        if sid == src_srv["id"]:
            continue
        try:
            counterpart = items.get(key)
            if not counterpart:
                continue
            cur_ud = counterpart.get("UserData") or {}
            cur = _norm(cur_ud)
            ours, theirs = (last_played or "")[:19], (cur_ud.get("LastPlayedDate") or "")[:19]
            if theirs and theirs > ours:
                continue        # the other side changed later: its own poll carries that change
            target = dict(cur)
            reasons = []
            if old is None:
                # First sight of this item, so we cannot tell what changed. Only a dated state
                # travels, and only over an older one (newest wins, as in the full merge).
                if not ours or ours == theirs:
                    continue
                target = dict(new)
                if not target["played"] and target["pos"] < MIN_PROGRESS:
                    target["pos"] = 0
                target["fav"] = cur["fav"] or new["fav"]
                reasons.append("newer state")
            elif new["played"] != old["played"]:
                if new["played"]:
                    target.update(played=True, pos=0)
                    reasons.append("marked played")
                elif new["pos"] >= MIN_PROGRESS:
                    target["pos"] = new["pos"]          # rewatch in progress: keep played as is
                    reasons.append("rewatch progress")
                else:
                    target.update(played=False, pos=0)
                    reasons.append("marked unplayed")
            elif new["pos"] != old["pos"] and not new["played"]:
                if new["pos"] >= MIN_PROGRESS or (old["pos"] or 0) >= MIN_PROGRESS:
                    target["pos"] = new["pos"] if new["pos"] >= MIN_PROGRESS else 0
                    secs = new["pos"] // 10_000_000
                    reasons.append(f"position {secs // 60}:{secs % 60:02d}")
            if old is not None and new["fav"] != old["fav"]:
                target["fav"] = new["fav"]
                reasons.append("favorite" if new["fav"] else "unfavorite")
            if target == cur:
                continue
            if sid not in playing_cache:
                playing_cache[sid] = await _playing(srv)
            if await _write(srv, other["jf_user_id"], counterpart, target, last_played, playing_cache[sid],
                            f"{', '.join(reasons)} (from {src_srv['name']}, {person})"):
                written.add((sid, counterpart["Id"]))
        except Exception as e:
            db.log(f"watch: {srv['name']}: {e}", "warn")


# --- full merge ------------------------------------------------------------------------
async def reconcile_rule(rule):
    """Bring every server in the rule to the newest state, per person and episode."""
    servers = _rule_servers(rule)
    if len(servers) < 2:
        return 0
    writes = 0
    for pid in _persons_for(rule):
        views = []   # (srv, account, {key: item})
        for sid, title_id in servers:
            acc, srv = _account(pid, sid), db.server(sid)
            if not acc or not srv or not srv["enabled"]:
                continue
            try:
                views.append((srv, acc, await _title_items(srv, acc["jf_user_id"], title_id)))
            except Exception as e:
                db.log(f"watch reconcile: {srv['name']}: {e}", "warn")
        if len(views) < 2:
            continue
        playing = {v[0]["id"]: await _playing(v[0]) for v in views}
        for key in set().union(*(v[2].keys() for v in views)):
            have = [(srv, acc, items[key]) for srv, acc, items in views if key in items]
            if len(have) < 2:
                continue
            def stamp(entry):   # second resolution: Jellyfin drops sub-second precision on write
                return ((entry[2].get("UserData") or {}).get("LastPlayedDate") or "")[:19]
            dated = [h for h in have if stamp(h)]
            if dated:
                winner = max(dated, key=stamp)
                want = _norm(winner[2]["UserData"])
                last = winner[2]["UserData"]["LastPlayedDate"]
                # favorites: set on any side wins (unfavorites travel through the poll)
                want["fav"] = any((h[2].get("UserData") or {}).get("IsFavorite") for h in have)
            else:   # nobody has a date: played on any side wins, no progress travels
                want = {"played": any((h[2].get("UserData") or {}).get("Played") for h in have), "pos": 0,
                        "fav": any((h[2].get("UserData") or {}).get("IsFavorite") for h in have)}
                last = None
            if not want["played"] and want["pos"] < MIN_PROGRESS:
                want["pos"] = 0
            for srv, acc, item in have:
                cur = _norm(item.get("UserData") or {})
                if cur == want:
                    _snap_put(srv["id"], acc["jf_user_id"], item["Id"], cur)
                    continue
                if not dated and not want["played"]:
                    continue   # never un-watch anything without a date to justify it
                try:
                    if await _write(srv, acc["jf_user_id"], item, want, last, playing[srv["id"]], "reconcile"):
                        writes += 1
                except Exception as e:
                    db.log(f"watch reconcile: {srv['name']}: {e}", "warn")
    return writes


async def reconcile_loop():
    while True:
        await reconcile_requested.wait()
        reconcile_requested.clear()
        if _rule_requests:
            ids = set(_rule_requests)
            _rule_requests.clear()
        else:
            ids = {r["id"] for r in db.q("SELECT id FROM rules WHERE enabled=1 AND sync_watch=1")}
        for rid in ids:
            rule = db.one("SELECT * FROM rules WHERE id=? AND enabled=1 AND sync_watch=1", (rid,))
            if not rule:
                continue
            try:
                # make sure targets know the title's id before merging
                from . import sync
                for t in db.q("SELECT * FROM targets WHERE rule_id=?", (rid,)):
                    await sync.match_title(t)
                n = await reconcile_rule(rule)
                if n:
                    db.log(f"watch reconcile '{rule['title']}': {n} update(s)")
            except Exception as e:
                db.log(f"watch reconcile '{rule['title']}': {e!r}", "error")
