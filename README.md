# jellymirror

Syncs selected titles (series or movies) between Jellyfin servers:

- **Files:** one way, from the title's source server to one or more targets. Each target
  has its own folder and path template, so its library can be organized differently.
  Changes are mirrored: new episodes are copied, replaced files are copied again, and
  files deleted on the source are deleted on the target.
- **Watch state:** in both directions, per person and per title, between every server in
  the rule: watched/unwatched, resume position and favorites. Matching uses provider ids
  (TVDB/TMDB/IMDB) and season/episode numbers, so paths and libraries can differ.

A FastAPI app with SQLite and a server-rendered UI, all in one container.

## Concepts

| Thing | What it is |
|---|---|
| **Server** | A Jellyfin URL and an admin API key, upload/download speed limits, plus *file access*: `local` (media mounted into the container), `ssh` (a host with rsync), or `none` (API only). *Path maps* translate the paths Jellyfin reports into paths jellymirror can reach. |
| **Person** | One viewer, linked to a user account on each server (Users page; "link users with the same name" does the obvious ones). |
| **Title** (rule) | A series or movie on its source server. |
| **Target** | A server the title syncs to: target folder (a library folder), path template, transfer method, delay, speed limit. "Watch only" targets sync watch state for a title the server already has. |
| **Job** | One copy or delete. Jobs can be paused, resumed, retried or cancelled. |

## How it works

**New media.** Every `lib_poll_seconds` (120 by default), each title's file list on its
source is fingerprinted (paths and sizes). A change marks its targets due after the
target's delay, and every further change pushes that back (debounce). Every
`reconcile_hours` (6 by default), all targets are checked regardless.

**Plan.** For a target, the source's files are rendered through the template into
destination paths and compared with the files jellymirror placed there before:

- missing, or changed on the source: copy
- placed by jellymirror and no longer on the source: delete
- already on the target with the same size: *adopted*, never deleted
- already on the target with a different size, not placed by jellymirror: left alone (warning)

Safety limits on deletes:

- Only files jellymirror copied itself are ever deleted.
- Nothing is deleted when the source lists no files for the title, or when it can't be
  reached.
- More than 5 deletes that are also more than half of the placed files are held until
  confirmed on the title page.

**Transfers.** Data never lands at the final name until it is complete. The partial file
lives in a hidden `.jellymirror-partial/` folder, which Jellyfin skips while scanning.

- `rsync` (needs file access on both ends): `--partial-dir` keeps the partial file, so a
  paused or interrupted copy resumes. ssh→ssh copies are relayed through `/staging`.
- `api`: `GET /Items/{id}/Download` with HTTP Range, appending to the partial file
  (locally, or through `ssh cat >>`). It resumes too, and restarts if the server
  answers from the wrong offset.
- `auto`: rsync when the source has file access, else the API.

**Speed limits** are in Mbit/s. A server has an upload limit (when it is the source) and a
download limit (when it receives files). Each is the server's total, split evenly between
its copies running at the same time. A target can add its own limit on top; the lowest
applies. API copies re-check their share every second. rsync copies take theirs when they
start, so a copy that starts alone keeps the whole limit until it finishes.

Every copy is size-checked, and the source must still have the planned size. Afterwards
the target scans the changed folders, or the whole library when the title is new to it,
because Jellyfin drops per-folder scans of folders it doesn't know.

Subtitles next to a video (same name stem, e.g. `ep.en.srt`) are copied and renamed
along with it when the source has file access. The API serves only the video.

**Watch state.** Every `watch_poll_seconds` (30 by default), each person's state of each
watch-synced title is read on every server in the rule. It is compared with the snapshot
of what was last seen or written there.

- A real change is carried to the other servers. The other side is re-read first, and
  nothing is written if it already matches, if it's being played right now, or if it
  changed more recently (by `LastPlayedDate`).
- Played → unplayed with a resume position is a rewatch: only the position travels.
- Positions under 60 s don't travel.
- PlayCount never goes down.
- Every write is read back.
- An item seen for the first time travels only if its date is newer than the other side's.
- The periodic merge: the newest `LastPlayedDate` wins; when nobody has one, played and
  favorite on any side win. It never un-watches without a date to justify it.

## Templates

Default series template: `{title} ({year})/Season {season:02}/{filename}`. Default movie
template: `{title} ({year})/{filename}`. Both can be changed in Settings or per target.

Variables:

| Variable | Value |
|---|---|
| `{title}`, `{year}` | the title's name and year |
| `{tvdb}`, `{tmdb}`, `{imdb}` | provider ids |
| `{season}`, `{episode}`, `{episode_end}` | numbers, formatted e.g. `{season:02}` |
| `{episode_title}` | the episode's name |
| `{filename}`, `{stem}`, `{ext}` | the source file's name, its name without extension, and the extension |
| `{rel}` | the file's path inside the title's folder on the source; mirrors its structure |

Example: `{title} ({year}) [tmdbid-{tmdb}]/{title} ({year}).{ext}`

## Jellyfin 10.11 notes (learned the hard way)

- `minDateLastSaved`, `SeriesIds` and `AncestorIds` are ignored: they return everything.
- `minDateLastSavedForUser` filters on the item's last scan, not on the user's data, so it
  can't detect watch changes.
- `ParentId=<series>` can return nothing on a freshly scanned server, so jellymirror then
  filters all episodes by `SeriesId` itself.
- The API key needs admin rights, or items come back without `Path`.
- Auth goes in the `Authorization` header only.

## Running

The container image holds only the runtime. The source is mounted at `/app`.

```bash
docker compose restart                # after a code change
docker compose up -d --build          # after requirements.txt changes
```

| Env | Default | |
|---|---|---|
| `JM_DATA` | `/data` | SQLite database and the generated ssh key (`/data/ssh/id_ed25519`) |
| `JM_STAGING` | `/data/staging` | Relay space for ssh→ssh copies |
| `JM_USER`, `JM_PASSWORD` | unset | Basic auth for the UI. Without both, there is no login. |

An ssh server needs jellymirror's public key (shown on the Servers page) in
`authorized_keys`, and rsync installed.

Ideas were borrowed (no code) from WatchState, CrossWatch, JellyPlex-Watched, the Server
Sync and Watch State Sync plugins, and JellySync.

## License

MIT, see [LICENSE](LICENSE).
