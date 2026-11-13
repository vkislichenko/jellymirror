"""Rule evaluation: what should exist on a target for a title, compared to what is there."""
import json
import posixpath
import time

from . import db, transfer
from .fileio import Files, to_fs
from .jellyfin import Jellyfin
from .layout import render

MASS_DELETE_MIN = 5          # more than this many deletes, and
MASS_DELETE_SHARE = 0.5      # more than this share of the placed files, needs confirmation


def default_template(item_type):
    return db.setting("series_template" if item_type == "Series" else "movie_template")


async def source_files(rule, src):
    """[(item, media_source_id, jellyfin_path, size)] for the title on its source server,
    or None if the title is gone (never treated as "delete everything")."""
    jf = Jellyfin(src)
    title = await jf.item(rule["source_item_id"])
    if not title:
        return None, None
    title["_title_pids"] = title.get("ProviderIds") or {}
    if rule["item_type"] == "Series":
        videos = await jf.episodes(rule["source_item_id"])
        title_path = title.get("Path")
    else:
        videos = [title]
        title_path = posixpath.dirname(title.get("Path") or "")
    if videos and not any(v.get("Path") for v in videos):
        raise RuntimeError(f"{src['name']} returns items without paths: its API key needs admin rights")
    out, seen = [], set()
    for v in videos:
        v["_title_pids"] = title["_title_pids"]
        for ms in v.get("MediaSources") or []:
            p = ms.get("Path")
            if not p or ms.get("Protocol") != "File" or p in seen:
                continue   # one file holding several episodes is listed once
            seen.add(p)
            out.append((v, ms.get("Id") or v["Id"], p, ms.get("Size")))
    return out, title_path


async def plan(target_id):
    """Compute copies/deletes for a target. Returns a dict; changes nothing."""
    tgt = db.one("SELECT * FROM targets WHERE id=?", (target_id,))
    rule = db.one("SELECT * FROM rules WHERE id=?", (tgt["rule_id"],))
    src, dst = db.server(rule["source_server_id"]), db.server(tgt["server_id"])
    result = {"copies": [], "deletes": [], "adopt": [], "warnings": [], "ok": 0}
    files, title_path = await source_files(rule, src)
    if files is None:
        result["warnings"].append(f"'{rule['title']}' is not on {src['name']} any more; nothing changed")
        return result
    template = tgt["template"] or default_template(rule["item_type"])
    src_files = Files(src) if src["access"] in ("local", "ssh") else None
    dst_files = Files(dst)

    desired = {}   # dest jellyfin path -> (src jellyfin path, size, source item id, is_sidecar)
    for item, ms_id, path, size in files:
        try:
            rel = render(template, rule, item, path, title_path)
        except (KeyError, ValueError) as e:
            result["warnings"].append(f"template error: {e}")
            return result
        dest = posixpath.join(tgt["root"], rel)
        if dest in desired:
            result["warnings"].append(f"two source files map to {dest}; check the template")
            continue
        desired[dest] = (path, size, ms_id, False)
        if src_files:   # subtitles next to the video, renamed alongside it
            src_stem = posixpath.splitext(posixpath.basename(path))[0]
            dst_stem = posixpath.splitext(dest)[0]
            for sc in await src_files.sidecars(to_fs(src, path)):
                suffix = posixpath.basename(sc)[len(src_stem):]
                sc_jf = posixpath.join(posixpath.dirname(path), posixpath.basename(sc))
                desired[dst_stem + suffix] = (sc_jf, await src_files.size(sc), None, True)

    placed = {p["dest_path"]: p for p in db.q("SELECT * FROM placed WHERE target_id=?", (target_id,))}
    for dest, (path, size, ms_id, sidecar) in desired.items():
        p = placed.get(dest)
        if p and p["source_path"] == path and p["source_size"] == size:
            result["ok"] += 1
            continue
        if p and not p["owned"]:
            result["warnings"].append(f"{dest} was already on {dst['name']} (not copied by jellymirror); "
                                      "left as is")
            continue
        if not p:
            existing = await dst_files.size(to_fs(dst, dest))
            if existing is not None:
                if existing == size:
                    result["adopt"].append(dest)
                else:
                    result["warnings"].append(f"{dest} already exists on {dst['name']} with a different "
                                              "size and was not copied by jellymirror; not overwritten")
                continue
        if sidecar and transfer.resolve_method(tgt["method"], src, dst) == "api":
            continue   # the API only serves the video itself
        result["copies"].append({"dest": dest, "src": path, "size": size, "item": ms_id,
                                 "replace": bool(p)})

    for dest, p in placed.items():
        if dest not in desired:
            if p["owned"]:
                result["deletes"].append({"dest": dest})
    owned = sum(1 for p in placed.values() if p["owned"])
    if not files:
        result["warnings"].append(f"{src['name']} lists no files for '{rule['title']}'; deletes held back")
        result["deletes"] = []
    result["mass_delete"] = (len(result["deletes"]) > MASS_DELETE_MIN
                             and len(result["deletes"]) > owned * MASS_DELETE_SHARE)
    return result


async def apply(target_id, confirm_deletes=False):
    tgt = db.one("SELECT * FROM targets WHERE id=?", (target_id,))
    try:
        await match_title(tgt)
        if not tgt["sync_files"]:
            _done(target_id, "watch only")
            return None
        p = await plan(target_id)
    except Exception as e:
        _done(target_id, f"error: {e}")
        db.log(f"target {target_id}: {e}", "error")
        return None
    for dest in p["adopt"]:
        src_info = next((c for c in p["copies"] if c["dest"] == dest), None)
        db.x("INSERT OR IGNORE INTO placed(target_id, dest_path, source_path, source_size, owned, created) "
             "VALUES (?,?,?,?,0,?)", (target_id, dest, src_info and src_info["src"], src_info and src_info["size"],
                                     time.time()))
    for c in p["copies"]:
        transfer.enqueue(target_id, "copy", c["dest"], c["src"], c["item"], c["size"], tgt["method"])
    held = p["mass_delete"] and not confirm_deletes
    if held:
        p["warnings"].append(f"{len(p['deletes'])} deletes held back: confirm them on the rule page")
    else:
        for d in p["deletes"]:
            transfer.enqueue(target_id, "delete", d["dest"])
    summary = (f"{p['ok']} in sync, {len(p['copies'])} to copy, "
               f"{0 if held else len(p['deletes'])} to delete" + (", " + "; ".join(p["warnings"]) if p["warnings"] else ""))
    _done(target_id, summary)
    for w in p["warnings"]:
        db.log(f"target {target_id}: {w}", "warn")
    return p


def _done(target_id, summary):
    db.x("UPDATE targets SET due_at=NULL, last_eval=?, last_result=? WHERE id=?",
         (time.time(), summary[:2000], target_id))


async def match_title(tgt):
    """Find (and remember) the title's item id on the target server."""
    rule = db.one("SELECT * FROM rules WHERE id=?", (tgt["rule_id"],))
    if tgt["matched_item_id"]:
        it = await Jellyfin(db.server(tgt["server_id"])).item(tgt["matched_item_id"])
        if it:
            return tgt["matched_item_id"]
    it = await Jellyfin(db.server(tgt["server_id"])).find_title(
        rule["item_type"], rule["title"], json.loads(rule["provider_ids"] or "{}"))
    mid = it["Id"] if it else None
    db.x("UPDATE targets SET matched_item_id=? WHERE id=?", (mid, tgt["id"]))
    return mid


def mark_due(target_id, now=False):
    tgt = db.one("SELECT delay_seconds FROM targets WHERE id=?", (target_id,))
    when = time.time() + (0 if now else tgt["delay_seconds"])
    # debounce: every new change pushes the run back by the delay
    db.x("UPDATE targets SET due_at=? WHERE id=?", (when, target_id))
