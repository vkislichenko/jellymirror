"""jellymirror web app: servers, users, per-title rules, jobs."""
import base64
import json
import os
import secrets
import shlex
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, fileio, scheduler, sync, transfer, watch
from .jellyfin import Jellyfin

app = FastAPI(title="jellymirror")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["ago"] = lambda ts: "" if not ts else (
    f"{int(time.time() - ts)}s ago" if time.time() - ts < 120 else
    f"{int((time.time() - ts) / 60)}m ago" if time.time() - ts < 7200 else
    time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)))
templates.env.globals["qs"] = lambda base, **kw: "?" + urlencode({k: v for k, v in {**base, **kw}.items()
                                                                  if v not in (None, "", 0)})
templates.env.filters["when"] = lambda ts: "" if not ts else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
templates.env.filters["mbps"] = lambda k: 0 if not k else round(k * 1024 * 8 / 1e6, 1)   # KiB/s -> Mbit/s
templates.env.filters["size"] = lambda n: "" if n is None else next(
    f"{n / 1024 ** i:.1f} {u}" for i, u in reversed(list(enumerate(["B", "KB", "MB", "GB", "TB"]))) if n >= 1024 ** i or i == 0)

AUTH_USER, AUTH_PASS = os.environ.get("JM_USER"), os.environ.get("JM_PASSWORD")
_bg = []


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if AUTH_USER and AUTH_PASS and request.url.path != "/health":
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, AUTH_USER) and secrets.compare_digest(p, AUTH_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="jellymirror"'})
    return await call_next(request)


@app.on_event("startup")
async def startup():
    db.connect()
    await fileio.ensure_ssh_key()
    _bg.extend(scheduler.start())
    db.log("jellymirror started")


def page(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})


def back(url):
    return RedirectResponse(url, status_code=303)


@app.get("/health")
async def health():
    return {"ok": True}


# --- dashboard -------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    return page(request, "index.html", servers=db.servers(), counts=counts,
                rules=db.one("SELECT COUNT(*) n FROM rules")["n"],
                active=_jobs("WHERE j.status IN ('running','queued','paused') ", 20),
                log=db.q("SELECT * FROM log ORDER BY id DESC LIMIT 15"))


# --- servers ---------------------------------------------------------------------------
@app.get("/servers", response_class=HTMLResponse)
async def servers(request: Request):
    return page(request, "servers.html", servers=db.servers(), pubkey=fileio.public_key())


@app.get("/servers/new", response_class=HTMLResponse)
@app.get("/servers/{sid}/edit", response_class=HTMLResponse)
async def server_form(request: Request, sid: int = None):
    srv = db.server(sid) if sid else {"access": "none", "path_maps": [], "ssh_port": 22, "enabled": 1}
    return page(request, "server_form.html", s=srv, pubkey=fileio.public_key())


def _parse_maps(text):
    maps = []
    for line in (text or "").splitlines():
        if "=" in line:
            a, b = (p.strip() for p in line.split("=", 1))
            if a and b:
                maps.append([a, b])
    return maps


def _kbps(mbps):
    """Mbit/s from a form -> KiB/s, the unit rsync's --bwlimit takes."""
    return max(0, round((mbps or 0) * 1e6 / 8 / 1024))


@app.post("/servers/save")
async def server_save(id: int = Form(None), name: str = Form(...), url: str = Form(...),
                      api_key: str = Form(...), access: str = Form("none"), ssh_host: str = Form(""),
                      ssh_port: int = Form(22), ssh_user: str = Form(""), path_maps: str = Form(""),
                      enabled: str = Form(None), up_mbps: float = Form(0), down_mbps: float = Form(0)):
    vals = (name.strip(), url.strip().rstrip("/"), api_key.strip(), access, ssh_host.strip() or None,
            ssh_port, ssh_user.strip() or None, json.dumps(_parse_maps(path_maps)), 1 if enabled else 0,
            _kbps(up_mbps), _kbps(down_mbps))
    if id:
        db.x("UPDATE servers SET name=?, url=?, api_key=?, access=?, ssh_host=?, ssh_port=?, ssh_user=?, "
             "path_maps=?, enabled=?, up_kbps=?, down_kbps=? WHERE id=?", vals + (id,))
    else:
        id = db.x("INSERT INTO servers(name, url, api_key, access, ssh_host, ssh_port, ssh_user, path_maps, "
                  "enabled, up_kbps, down_kbps) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
    return back(f"/servers/{id}/test")


@app.get("/servers/{sid}/test", response_class=HTMLResponse)
async def server_test(request: Request, sid: int):
    srv = db.server(sid)
    checks = []
    try:
        info = await Jellyfin(srv).info()
        checks.append(("API", True, f"{info.get('ServerName')} — Jellyfin {info.get('Version')}"))
        libs = await Jellyfin(srv).libraries()
    except Exception as e:
        checks.append(("API", False, str(e)))
        libs = []
    for lib in libs:
        for loc in lib.get("Locations") or []:
            fs = fileio.to_fs(srv, loc)
            if srv["access"] == "none":
                checks.append((f"Library {lib['Name']}", None, f"{loc} (no file access configured)"))
                continue
            try:
                files = fileio.Files(srv)
                if srv["access"] == "local":
                    exists = os.path.isdir(fs)
                else:
                    exists = (await fileio.sh(srv, f"test -d {shlex.quote(fs)}", check=False))[0] == 0
                n = len(await files.listdir(fs)) if exists else 0
                checks.append((f"Library {lib['Name']}", exists,
                               f"{loc} → {fs}: {n} entries" if exists else
                               f"{loc} → {fs}: folder not found; check the path maps"))
            except Exception as e:
                checks.append((f"Library {lib['Name']}", False, f"{loc} → {fs}: {e}"))
    if srv["access"] == "ssh":
        try:
            _, out, _ = await fileio.sh(srv, "rsync --version | head -1")
            checks.append(("rsync on host", True, out.strip()))
        except Exception as e:
            checks.append(("rsync on host", False, str(e)))
    db.x("UPDATE servers SET status=?, status_at=? WHERE id=?",
         ("ok" if checks and checks[0][1] else "unreachable", time.time(), sid))
    return page(request, "server_test.html", s=srv, checks=checks)


@app.post("/servers/{sid}/delete")
async def server_delete(sid: int):
    db.x("DELETE FROM servers WHERE id=?", (sid,))
    return back("/servers")


# --- users -----------------------------------------------------------------------------
@app.get("/users", response_class=HTMLResponse)
async def users(request: Request):
    per_server = []
    for srv in db.servers(enabled_only=True):
        try:
            us = await Jellyfin(srv).users()
        except Exception as e:
            us, srv["error"] = [], str(e)
        per_server.append((srv, us))
    persons = db.q("SELECT * FROM persons ORDER BY name")
    accounts = db.q("SELECT a.*, s.name server FROM accounts a JOIN servers s ON s.id=a.server_id")
    linked = {(a["server_id"], a["jf_user_id"]): a for a in accounts}
    return page(request, "users.html", per_server=per_server, persons=persons, accounts=accounts, linked=linked)


@app.post("/users/link")
async def users_link(server_id: int = Form(...), jf_user_id: str = Form(...), jf_user_name: str = Form(""),
                     person_id: str = Form(...), new_person: str = Form("")):
    if person_id == "new":
        name = new_person.strip() or jf_user_name
        db.x("INSERT OR IGNORE INTO persons(name) VALUES (?)", (name,))
        person_id = db.one("SELECT id FROM persons WHERE name=?", (name,))["id"]
    db.x("INSERT INTO accounts(person_id, server_id, jf_user_id, jf_user_name) VALUES (?,?,?,?) "
         "ON CONFLICT(server_id, jf_user_id) DO UPDATE SET person_id=excluded.person_id",
         (int(person_id), server_id, jf_user_id, jf_user_name))
    return back("/users")


@app.post("/users/automap")
async def users_automap():
    """Link users with the same name (case-insensitive) on every server to one person."""
    for srv in db.servers(enabled_only=True):
        try:
            us = await Jellyfin(srv).users()
        except Exception:
            continue
        for u in us:
            name = u["Name"]
            p = db.one("SELECT id FROM persons WHERE lower(name)=lower(?)", (name,))
            pid = p["id"] if p else db.x("INSERT INTO persons(name) VALUES (?)", (name,))
            db.x("INSERT OR IGNORE INTO accounts(person_id, server_id, jf_user_id, jf_user_name) VALUES (?,?,?,?)",
                 (pid, srv["id"], u["Id"], name))
    return back("/users")


@app.post("/users/unlink/{aid}")
async def users_unlink(aid: int):
    db.x("DELETE FROM accounts WHERE id=?", (aid,))
    db.x("DELETE FROM persons WHERE id NOT IN (SELECT person_id FROM accounts)")
    return back("/users")


# --- rules -----------------------------------------------------------------------------
@app.get("/rules", response_class=HTMLResponse)
async def rules(request: Request):
    rows = db.q("SELECT r.*, s.name source FROM rules r JOIN servers s ON s.id=r.source_server_id ORDER BY r.title")
    for r in rows:
        r["targets"] = db.q("SELECT t.*, s.name server FROM targets t JOIN servers s ON s.id=t.server_id "
                            "WHERE rule_id=?", (r["id"],))
    return page(request, "rules.html", rules=rows)


BROWSE_PAGE = 60
BROWSE_LIBS = (None, "tvshows", "movies", "mixed", "homevideos")


@app.get("/rules/new", response_class=HTMLResponse)
async def rule_new(request: Request, server_id: int = None, q: str = "", parent: str = "", kind: str = "",
                   letter: str = "", p: int = 0):
    """Title picker. Without a parent or filter: the server's libraries. Inside a library or
    folder: its direct children (folders open, titles can be picked). With a name, letter or
    type filter: every matching title under the current parent."""
    servers = db.servers(enabled_only=True)
    if server_id is None and servers:
        server_id = servers[0]["id"]
    results, total, libs, crumbs, error = [], 0, [], [], None
    filtered = bool(q.strip() or letter or kind in ("Series", "Movie"))
    types = kind if kind in ("Series", "Movie") else "Series,Movie"
    p = max(p, 0)
    if server_id:
        jf = Jellyfin(db.server(server_id))
        try:
            libs = [l for l in await jf.libraries() if l.get("CollectionType") in BROWSE_LIBS]
            if parent:
                crumbs = list(reversed(await jf.ancestry(parent, await jf.libraries())))
            if parent or filtered:
                results, total = await jf.browse(parent or None, types if filtered else types + ",Folder",
                                                 q.strip() or None, letter or None, p * BROWSE_PAGE,
                                                 BROWSE_PAGE, recursive=filtered)
        except Exception as e:
            error = str(e)
    existing = {(r["source_server_id"], r["source_item_id"]): r["id"] for r in db.q("SELECT * FROM rules")}
    pages = (total + BROWSE_PAGE - 1) // BROWSE_PAGE
    state = dict(server_id=server_id, q=q, parent=parent, kind=kind, letter=letter)
    return page(request, "rule_new.html", servers=servers, libs=libs, results=results, total=total, p=p,
                pages=pages, error=error, existing=existing, state=state, crumbs=crumbs, filtered=filtered,
                letters="#ABCDEFGHIJKLMNOPQRSTUVWXYZ", **state)


@app.get("/img/{server_id}/{item_id}")
async def item_image(server_id: int, item_id: str, tag: str = ""):
    """Item art proxied through jellymirror, so the browser needs neither the server's
    address nor its API key. `tag` only busts the browser cache when the art changes."""
    srv = db.server(server_id)
    got = await Jellyfin(srv, timeout=15).image(item_id) if srv else None
    if not got:
        return Response(status_code=404)
    return Response(got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=604800"})


@app.post("/rules/create")
async def rule_create(server_id: int = Form(...), item_id: str = Form(...)):
    it = await Jellyfin(db.server(server_id)).item(item_id)
    if not it or it.get("Type") not in ("Series", "Movie"):
        return back(f"/rules/new?server_id={server_id}")
    row = db.one("SELECT id FROM rules WHERE source_server_id=? AND source_item_id=?", (server_id, item_id))
    if row:
        return back(f"/rules/{row['id']}")
    rid = db.x("INSERT INTO rules(source_server_id, source_item_id, item_type, title, year, provider_ids, created) "
               "VALUES (?,?,?,?,?,?,?)", (server_id, item_id, it["Type"], it["Name"], it.get("ProductionYear"),
                                          json.dumps(it.get("ProviderIds") or {}), time.time()))
    db.log(f"rule created for '{it['Name']}'")
    return back(f"/rules/{rid}")


@app.get("/rules/{rid}", response_class=HTMLResponse)
async def rule_page(request: Request, rid: int, edit: int = None):
    rule = db.one("SELECT r.*, s.name source FROM rules r JOIN servers s ON s.id=r.source_server_id WHERE r.id=?", (rid,))
    if not rule:
        return back("/rules")
    rule["provider_ids"] = json.loads(rule["provider_ids"] or "{}")
    targets = db.q("SELECT t.*, s.name server, s.access FROM targets t JOIN servers s ON s.id=t.server_id "
                   "WHERE rule_id=? ORDER BY s.name", (rid,))
    for t in targets:
        t["placed"] = db.one("SELECT COUNT(*) n, SUM(source_size) bytes FROM placed WHERE target_id=?", (t["id"],))
    used = {t["server_id"] for t in targets} | {rule["source_server_id"]}
    free = [s for s in db.servers(enabled_only=True) if s["id"] not in used]
    editing = next((t for t in targets if t["id"] == edit), None)
    form_srv = db.server(editing["server_id"]) if editing else (free[0] if free else None)
    roots = []
    for srv in ([form_srv] if editing else free):
        try:
            for lib in await Jellyfin(srv).libraries():
                for loc in lib.get("Locations") or []:
                    roots.append((srv["id"], lib["Name"], loc))
        except Exception:
            pass
    persons = db.q("SELECT * FROM persons ORDER BY name")
    chosen = {r["person_id"] for r in db.q("SELECT person_id FROM rule_users WHERE rule_id=?", (rid,))}
    return page(request, "rule.html", rule=rule, targets=targets, free=free, roots=roots, editing=editing,
                persons=persons, chosen=chosen, default_template=sync.default_template(rule["item_type"]),
                jobs=_jobs("WHERE t.rule_id=? ", 30, (rid,)))


@app.post("/rules/{rid}/target")
async def target_save(rid: int, target_id: int = Form(None), server_id: int = Form(None),
                      sync_files: str = Form(None), root: str = Form(""), template: str = Form(""),
                      method: str = Form("auto"), delay_seconds: int = Form(300), bw_mbps: float = Form(0)):
    bwlimit_kbps = _kbps(bw_mbps)
    vals = (1 if sync_files else 0, root.strip().rstrip("/") or None, template.strip() or None, method,
            delay_seconds, bwlimit_kbps)
    if target_id:
        db.x("UPDATE targets SET sync_files=?, root=?, template=?, method=?, delay_seconds=?, bwlimit_kbps=? "
             "WHERE id=?", vals + (target_id,))
    else:
        target_id = db.x("INSERT INTO targets(sync_files, root, template, method, delay_seconds, bwlimit_kbps, "
                         "rule_id, server_id) VALUES (?,?,?,?,?,?,?,?)", vals + (rid, server_id))
    if vals[0] and not vals[1]:
        db.x("UPDATE targets SET sync_files=0, last_result='no target folder set: files are not synced' WHERE id=?",
             (target_id,))
    sync.mark_due(target_id, now=True)
    return back(f"/rules/{rid}")


@app.get("/rules/{rid}/target/{tid}/preview", response_class=HTMLResponse)
async def target_preview(request: Request, rid: int, tid: int):
    try:
        p = await sync.plan(tid)
    except Exception as e:
        p = {"copies": [], "deletes": [], "adopt": [], "warnings": [f"error: {e}"], "ok": 0}
    tgt = db.one("SELECT t.*, s.name server FROM targets t JOIN servers s ON s.id=t.server_id WHERE t.id=?", (tid,))
    rule = db.one("SELECT * FROM rules WHERE id=?", (rid,))
    return page(request, "preview.html", p=p, t=tgt, rule=rule)


@app.post("/rules/{rid}/target/{tid}/delete")
async def target_delete(rid: int, tid: int):
    db.x("DELETE FROM targets WHERE id=?", (tid,))
    return back(f"/rules/{rid}")


@app.post("/rules/{rid}/target/{tid}/confirm-deletes")
async def target_confirm(rid: int, tid: int):
    await sync.apply(tid, confirm_deletes=True)
    return back(f"/rules/{rid}")


@app.post("/rules/{rid}/sync")
async def rule_sync(rid: int):
    for t in db.q("SELECT id FROM targets WHERE rule_id=?", (rid,)):
        sync.mark_due(t["id"], now=True)
    watch.request_rule(rid)
    return back(f"/rules/{rid}")


@app.post("/rules/{rid}/users")
async def rule_users(request: Request, rid: int):
    form = await request.form()
    sync_watch = 1 if form.get("sync_watch") else 0
    db.x("UPDATE rules SET sync_watch=? WHERE id=?", (sync_watch, rid))
    db.x("DELETE FROM rule_users WHERE rule_id=?", (rid,))
    for pid in form.getlist("person"):
        db.x("INSERT INTO rule_users(rule_id, person_id) VALUES (?,?)", (rid, int(pid)))
    if sync_watch:
        watch.request_rule(rid)
    return back(f"/rules/{rid}")


@app.post("/rules/{rid}/toggle")
async def rule_toggle(rid: int):
    db.x("UPDATE rules SET enabled=1-enabled WHERE id=?", (rid,))
    return back(f"/rules/{rid}")


@app.post("/rules/{rid}/delete")
async def rule_delete(rid: int):
    db.x("DELETE FROM rules WHERE id=?", (rid,))   # copied files stay on the targets
    return back("/rules")


# --- jobs ------------------------------------------------------------------------------
def _jobs(where="", limit=200, args=()):
    return db.q("SELECT j.*, s.name server, r.title FROM jobs j JOIN targets t ON t.id=j.target_id "
                "JOIN servers s ON s.id=t.server_id JOIN rules r ON r.id=t.rule_id " + where +
                "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 "
                "WHEN 'failed' THEN 3 ELSE 4 END, j.id DESC LIMIT ?", tuple(args) + (limit,))


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, status: str = ""):
    where = "WHERE j.status=? " if status else ""
    return page(request, "jobs.html", jobs=_jobs(where, 300, (status,) if status else ()), status=status)


@app.post("/jobs/{jid}/{action}")
async def job_action(jid: int, action: str, request: Request):
    {"pause": transfer.pause, "resume": transfer.resume, "cancel": transfer.cancel}[action](jid)
    return back(request.headers.get("referer") or "/jobs")


@app.post("/jobs-bulk/{action}")
async def jobs_bulk(action: str, request: Request):
    if action == "clear":
        db.x("DELETE FROM jobs WHERE status IN ('done','cancelled')")
    elif action == "pause-all":
        for j in db.q("SELECT id FROM jobs WHERE status IN ('queued','running')"):
            transfer.pause(j["id"])
    elif action == "resume-all":
        for j in db.q("SELECT id FROM jobs WHERE status='paused'"):
            transfer.resume(j["id"])
    elif action == "retry-failed":
        for j in db.q("SELECT id FROM jobs WHERE status='failed'"):
            transfer.resume(j["id"])
    return back(request.headers.get("referer") or "/jobs")


# --- log & settings --------------------------------------------------------------------
@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, level: str = ""):
    rows = db.q("SELECT * FROM log " + ("WHERE level=? " if level else "") + "ORDER BY id DESC LIMIT 500",
                (level,) if level else ())
    return page(request, "log.html", rows=rows, level=level)


SETTING_KEYS = ["lib_poll_seconds", "watch_poll_seconds", "reconcile_hours", "max_parallel_jobs",
                "series_template", "movie_template"]


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return page(request, "settings.html", values={k: db.setting(k) for k in SETTING_KEYS},
                pubkey=fileio.public_key())


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for k in SETTING_KEYS:
        if k in form and str(form[k]).strip():
            db.set_setting(k, str(form[k]).strip())
    return back("/settings")


@app.post("/reconcile")
async def reconcile_now():
    scheduler.reconcile_all()
    return back("/")
