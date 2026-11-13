"""Where a source file lands on a target: a per-target path template."""
import posixpath
import re

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean(s):
    """Make a string safe as one path component."""
    s = _BAD.sub(" ", str(s or "")).strip().rstrip(".")
    return re.sub(r"\s+", " ", s) or "_"


class _Vars(dict):
    def __missing__(self, key):
        raise KeyError(f"unknown template variable {{{key}}}")


def render(template, rule, item, src_path, title_path):
    """Relative destination path for one source file.

    Variables: title, year, tvdb, tmdb, imdb, season, episode, episode_end, episode_title,
    filename, stem, ext, rel (path of the file relative to the title's folder on the source).
    """
    pids = {k.lower(): v for k, v in (item.get("_title_pids") or {}).items()}
    filename = posixpath.basename(src_path)
    stem, ext = posixpath.splitext(filename)
    rel = posixpath.relpath(src_path, title_path) if title_path and src_path.startswith(title_path + "/") else filename
    v = _Vars(
        title=clean(rule["title"]), year=rule["year"] or "",
        tvdb=pids.get("tvdb", ""), tmdb=pids.get("tmdb", ""), imdb=pids.get("imdb", ""),
        season=item.get("ParentIndexNumber") or 0, episode=item.get("IndexNumber") or 0,
        episode_end=item.get("IndexNumberEnd") or item.get("IndexNumber") or 0,
        episode_title=clean(item.get("Name")), filename=filename, stem=stem, ext=ext.lstrip("."),
        rel=rel,
    )
    out = template.format_map(v)
    out = out.replace(" ()", "").replace("()", "")     # empty {year}
    parts = [p for p in out.split("/") if p not in ("", ".", "..")]
    # every component except those coming from {rel}/{filename} is already clean
    return "/".join(p.strip() for p in parts)
