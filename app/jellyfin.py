"""Thin async client for the parts of the Jellyfin API jellymirror uses.

Notes from testing 10.11 and from other sync tools:
- Auth goes in the Authorization header only (mixing api_key/X-Emby-Token is undefined).
- Unknown query parameters are silently ignored. On 10.11 minDateLastSaved, SeriesIds and
  AncestorIds are ignored (everything comes back), and minDateLastSavedForUser filters on
  the item's last scan, not on the user's data: none of them is used.
- TotalRecordCount is unreliable: page with EnableTotalRecordCount=false until a short page.
- A non-admin API key gets items without Path.
"""
import httpx

FIELDS = "Path,ProviderIds,MediaSources,ProductionYear,ParentId,DateCreated"
PAGE = 500


def norm_ids(pids):
    """Provider ids, keys lowercased, numeric ids without leading zeros, blanks dropped."""
    out = {}
    for k, v in (pids or {}).items():
        v = str(v or "").strip()
        if not v:
            continue
        k = k.lower()
        if v.isdigit():
            v = v.lstrip("0") or "0"
        out[k] = v.lower()
    return out


def ids_agree(a, b):
    """Same title when they share at least one id and no shared id disagrees."""
    a, b = norm_ids(a), norm_ids(b)
    shared = set(a) & set(b)
    return bool(shared) and all(a[k] == b[k] for k in shared)


class Jellyfin:
    def __init__(self, srv, timeout=30):
        self.srv = srv
        self.base = srv["url"].rstrip("/")
        self.headers = {"Authorization": 'MediaBrowser Client="jellymirror", Device="jellymirror", '
                                         f'DeviceId="jellymirror", Version="0.1", Token="{srv["api_key"]}"'}
        self.timeout = timeout

    def client(self, timeout=None):
        return httpx.AsyncClient(base_url=self.base, headers=self.headers,
                                 timeout=timeout or self.timeout, follow_redirects=True)

    async def get(self, path, **params):
        async with self.client() as c:
            r = await c.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json() if r.content else None

    async def post(self, path, json=None, **params):
        async with self.client() as c:
            r = await c.post(path, json=json, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    async def paged(self, **params):
        out, start = [], 0
        while True:
            d = await self.get("/Items", StartIndex=start, Limit=PAGE, EnableTotalRecordCount="false", **params)
            items = d.get("Items") or []
            out += items
            start += len(items)
            if len(items) < PAGE:
                return out

    # --- server -----------------------------------------------------------------------
    async def info(self):
        return await self.get("/System/Info")

    async def users(self):
        return await self.get("/Users")

    async def libraries(self):
        return await self.get("/Library/VirtualFolders")

    # --- items ------------------------------------------------------------------------
    async def item(self, item_id):
        d = await self.get("/Items", ids=item_id, Fields=FIELDS, EnableImages="false")
        items = d.get("Items") or []
        return items[0] if items else None

    async def search(self, term, types="Series,Movie", limit=30):
        d = await self.get("/Items", searchTerm=term, IncludeItemTypes=types, Recursive="true",
                           Fields=FIELDS, Limit=limit, EnableImages="false")
        return d.get("Items") or []

    async def all_of_type(self, types):
        return await self.paged(IncludeItemTypes=types, Recursive="true", Fields="ProviderIds,ProductionYear",
                                EnableImages="false")

    async def series_episodes(self, series_id, **params):
        """Episodes of a series. ParentId=<series> is the cheap way, but a freshly scanned
        10.11 server can return nothing for it (and SeriesIds/AncestorIds are ignored), so an
        empty answer falls back to filtering every episode by SeriesId here."""
        params = dict(Recursive="true", IncludeItemTypes="Episode", EnableImages="false", IsMissing="false",
                      **params)
        items = await self.paged(ParentId=series_id, **params)
        if not items:
            items = [i for i in await self.paged(**params) if i.get("SeriesId") == series_id]
        return items

    async def episodes(self, series_id):
        """Real (on-disk) episodes of a series."""
        items = await self.series_episodes(series_id, Fields=FIELDS)
        return [i for i in items if i.get("LocationType") != "Virtual"]

    async def find_title(self, item_type, title, provider_ids):
        """Find a series/movie on this server: shared provider ids must all agree."""
        if not norm_ids(provider_ids):
            return None
        for it in await self.search(title, item_type):
            if it.get("Type") == item_type and ids_agree(it.get("ProviderIds"), provider_ids):
                return it
        for it in await self.all_of_type(item_type):
            if ids_agree(it.get("ProviderIds"), provider_ids):
                return it
        return None

    # --- user data --------------------------------------------------------------------
    async def title_user_items(self, user_id, title_id):
        """A user's view (with UserData) of every episode of a series, or of one movie."""
        d = await self.get("/Items", userId=user_id, ids=title_id, Fields="ProviderIds", EnableImages="false")
        title = (d.get("Items") or [None])[0]
        if not title:
            return []
        if title.get("Type") != "Series":
            return [title]
        return await self.series_episodes(title_id, userId=user_id, Fields="ProviderIds")

    async def user_data(self, user_id, item_id):
        return await self.get(f"/UserItems/{item_id}/UserData", userId=user_id)

    async def set_user_data(self, user_id, item_id, data):
        return await self.post(f"/UserItems/{item_id}/UserData", json=data, userId=user_id)

    # --- library ----------------------------------------------------------------------
    async def media_updated(self, updates):
        """updates: [(path, 'Created'|'Modified'|'Deleted')] -> targeted scan of those paths."""
        await self.post("/Library/Media/Updated",
                        json={"Updates": [{"Path": p, "UpdateType": t} for p, t in updates]})

    async def refresh_library(self):
        await self.post("/Library/Refresh")
