"""Navigatore completo del sito DAZN - scopre TUTTE le sezioni e i contenuti dinamicamente."""
import asyncio
from typing import List, Dict, Optional
from dataclasses import dataclass
from dazn_navigator2.api.client import DaznClient, DaznAPIError


@dataclass
class RailSection:
    id: str
    title: str
    tiles_count: int
    params: str = ""


@dataclass
class ContentTile:
    id: str
    asset_id: str
    title: str
    description: str
    section: str
    tile_type: str
    image: str
    raw: dict


class DaznExplorer:
    """Esplora l'intero sito DAZN: scopre rails, naviga sezioni, estrae contenuti."""

    def __init__(self):
        self.client = DaznClient()
        self._sections: List[RailSection] = []
        self._cached_tiles: Dict[str, List[ContentTile]] = {}
        # Ultimo errore di API incontrato da get_tiles. get_tiles continua a
        # restituire [] per compatibilita' (diversi chiamanti si appoggiano a
        # questo comportamento), ma senza questo campo non si distingue "DAZN
        # risponde e non c'e' niente in onda" da "DAZN ha risposto con un
        # errore": sono due situazioni opposte che finivano identiche.
        self.last_error: Optional[str] = None

    async def discover_all(self) -> List[RailSection]:
        sections = {}

        await self._discover_via_v9_home(sections)

        await self._discover_extended_categories(sections)

        await self._discover_via_performance_api(sections)

        fixed_rails_order = [
            ("Live",            "Live - In Diretta"),
            ("Catchup",         "CatchUp - Replay & VOD"),
            ("LinearChannels",  "TV Live - Canali Lineari"),
            ("LiveAndNextNew",  "Live e Prossimi Eventi"),
            ("Sport",           "Sport - Tutti gli Sport"),
            ("Livetvschedule",  "Guida TV"),
            ("ExcludeMultiview","Tutti i live"),
        ]

        known_titles = {
            "Livetvschedule": "Guida TV",
            "ExcludeMultiview": "Tutti i live",
            "multiview": "Multiview",
            "LinearChannels": "TV Live - Canali Lineari",
            "Catchup": "CatchUp - Replay & VOD",
            "LiveAndNextNew": "Live e Prossimi Eventi",
            "Sport": "Sport - Tutti gli Sport",
            "Live": "Live - In Diretta"
        }

        for rid, title in fixed_rails_order:
            if rid not in sections:
                sections[rid] = RailSection(id=rid, title=title, tiles_count=0)

        final_list = []
        for rid, _ in fixed_rails_order:
            if rid in sections:
                sec = sections.pop(rid)
                if sec.title == rid or sec.id in known_titles:
                    sec.title = known_titles.get(sec.id, sec.title)
                if sec.tiles_count > 0:
                    final_list.append(sec)

        cat_competizioni = []
        cat_gratis = []
        cat_news_doc = []
        cat_live_main = []
        cat_altro = []

        for sec in sections.values():
            if sec.tiles_count == 0:
                continue
            t = sec.title.lower()
            if any(x in t for x in ["serie a", "serie b", "la liga", "europa", "uefa"]):
                cat_competizioni.append(sec)
            elif "gratis" in t:
                cat_gratis.append(sec)
            elif any(x in t for x in ["news", "documentari", "original"]):
                cat_news_doc.append(sec)
            elif any(x in t for x in ["live", "ora", "prossimi"]):
                cat_live_main.append(sec)
            else:
                cat_altro.append(sec)

        cat_competizioni.sort(key=lambda s: s.title)
        cat_gratis.sort(key=lambda s: s.title)
        cat_news_doc.sort(key=lambda s: s.title)
        cat_live_main.sort(key=lambda s: s.title)
        cat_altro.sort(key=lambda s: s.title)

        final_list.extend(cat_live_main)
        final_list.extend(cat_competizioni)
        final_list.extend(cat_news_doc)
        final_list.extend(cat_gratis)
        final_list.extend(cat_altro)
        return final_list

    async def _discover_extended_categories(self, sections: Dict[str, RailSection]):
        extended_ids = {
            "Football": "Calcio", "SerieA": "Serie A", "SerieB": "Serie B", "LaLiga": "La Liga",
            "Basketball": "Basket", "Tennis": "Tennis", "Motorsport": "Motori",
            "NFL": "NFL", "AmericanFootball": "Football Americano", "Volleyball": "Pallavolo",
            "RugbyUnion": "Rugby", "Darts": "Freccette", "Snooker": "Snooker",
            "Boxing": "Boxe", "MMA": "MMA / Arti Marziali", "Cycling": "Ciclismo",
            "eSports": "eSports", "Documentaries": "Documentari", "Originals": "DAZN Originals",
            "Live": "Live", "Catchup": "Catchup - Replay", "LiveAndNextNew": "Live e Prossimi Eventi"
        }

        sem = asyncio.Semaphore(3)
        try:
            from dazn_navigator2.services.browser import get_browser
            b = await get_browser()
            await b.ensure_session()

            async def _fetch_cat(cat_id, title):
                async with sem:
                    url = (
                        f"https://rails.discovery.indazn.com/eu/v9/rails"
                        f"?groupId={cat_id}&params=PageType:{cat_id}&country=it&brand=dazn"
                    )
                    try:
                        res = await b.fetch_json(url)
                        if res and res.get("ok"):
                            data = res.get("data", {})
                            rails = data.get("Rails", [])
                            tiles = []
                            for r in rails:
                                tiles.extend(r.get("Tiles", []))
                            if tiles:
                                sections[cat_id] = RailSection(id=cat_id, title=title, tiles_count=len(tiles))
                                self._cache_tiles(cat_id, title, tiles)
                    except Exception:
                        pass

            tasks = [_fetch_cat(cid, t) for cid, t in extended_ids.items() if cid not in sections]
            await asyncio.gather(*tasks)
        except Exception:
            pass

    async def _discover_via_v9_home(self, sections: Dict[str, RailSection]):
        try:
            from dazn_navigator2.services.browser import get_browser
            b = await get_browser()
            await b.ensure_session()
            v9_url = (
                "https://rails.discovery.indazn.com/eu/v9/rails"
                "?groupId=Home&params=PageType:Home&country=it&brand=dazn"
            )
            result = await b.fetch_json(v9_url)
            if not result.get("ok"):
                return

            raw_rails = result["data"].get("Rails", [])
            if not raw_rails:
                return

            sem = asyncio.Semaphore(5)

            async def _load_rail(rid: str, title: str, inline_tiles: list):
                async with sem:
                    if inline_tiles:
                        sections[rid] = RailSection(id=rid, title=title, tiles_count=len(inline_tiles))
                        self._cache_tiles(rid, title, inline_tiles)
                        return
                    try:
                        data = await self.client.get(
                            f"/Rail?platform=web&id={rid}&country=it&brand=dazn"
                            f"&languageCode=it&params=PageType:Home"
                        )
                        tiles = data.get("Tiles", [])
                        if not tiles and "Rails" in data:
                            tiles = [t for r in data["Rails"] for t in r.get("Tiles", [])]
                        if not title or title == rid:
                            title = data.get("Title", rid)
                        if tiles:
                            sections[rid] = RailSection(id=rid, title=title, tiles_count=len(tiles))
                            self._cache_tiles(rid, title, tiles)
                        else:
                            sections[rid] = RailSection(id=rid, title=title, tiles_count=0)
                    except Exception:
                        pass

            tasks = []
            for rail in raw_rails:
                rid = rail.get("Id", "").strip()
                if not rid:
                    continue
                title = (rail.get("Title") or rail.get("Name") or "").strip()
                inline_tiles = rail.get("Tiles", [])
                tasks.append(_load_rail(rid, title, inline_tiles))

            await asyncio.gather(*tasks)
        except Exception:
            pass

    async def _discover_via_performance_api(self, sections: Dict[str, RailSection]):
        try:
            from dazn_navigator2.services.browser import get_browser
            b = await get_browser()
            raw_ids = await b.page.evaluate("""
                () => {
                    const entries = performance.getEntriesByType('resource');
                    const seen = new Set();
                    const result = [];
                    for (const e of entries) {
                        if (!e.name.includes('/Rail?') && !e.name.includes('/v9/rails')) continue;
                        const m = e.name.match(/[?&]id=([^&]+)/);
                        if (m) {
                            const rid = decodeURIComponent(m[1]);
                            if (!seen.has(rid)) { seen.add(rid); result.push(rid); }
                        }
                    }
                    return result;
                }
            """)
            for rid in raw_ids:
                if rid not in sections:
                    try:
                        data = await self.client.get(
                            f"/Rail?platform=web&id={rid}&country=it&brand=dazn&languageCode=it"
                        )
                        tiles = data.get("Tiles", [])
                        if not tiles and "Rails" in data:
                            tiles = [t for r in data["Rails"] for t in r.get("Tiles", [])]
                        title = data.get("Title") or rid
                        sections[rid] = RailSection(id=rid, title=title, tiles_count=len(tiles))
                        if tiles:
                            self._cache_tiles(rid, title, tiles)
                    except Exception:
                        continue
        except Exception:
            pass

    def _cache_tiles(self, rail_id: str, section_title: str, tiles: List[dict]):
        items = []
        for t in tiles:
            ct = ContentTile(
                id=t.get("Id", ""),
                asset_id=t.get("AssetId", ""),
                title=t.get("Title", "Senza titolo"),
                description=t.get("Description", ""),
                section=section_title,
                tile_type=t.get("Type", "Unknown"),
                image=t.get("Image", "") or t.get("HeroImage", "") or "",
                raw=t,
            )
            items.append(ct)
        self._cached_tiles[rail_id] = items

    async def get_tiles(self, section_id: str) -> List[ContentTile]:
        if section_id in self._cached_tiles:
            return self._cached_tiles[section_id]

        if section_id == "epg":
            return await self._fetch_epg()
        if section_id == "schedule":
            return await self._fetch_schedule()
        if section_id == "news":
            return await self._fetch_news()

        try:
            data = await self.client.get(
                f"/Rail?platform=web&id={section_id}&country=it&brand=dazn&languageCode=it&params=PageType:Home"
            )
            tiles = data.get("Tiles", [])
            if not tiles and "Rails" in data:
                tiles = [t for r in data["Rails"] for t in r.get("Tiles", [])]
            items = []
            for t in tiles:
                ct = ContentTile(
                    id=t.get("Id", ""),
                    asset_id=t.get("AssetId", ""),
                    title=t.get("Title", "Senza titolo"),
                    description=t.get("Description", ""),
                    section=section_id,
                    tile_type=t.get("Type", "Unknown"),
                    image=t.get("Image", "") or t.get("HeroImage", "") or "",
                    raw=t,
                )
                items.append(ct)
            self._cached_tiles[section_id] = items
            self.last_error = None
            return items
        except DaznAPIError as e:
            # Non propagare: alcuni chiamanti (VOD, self-healing, diagnosi)
            # trattano [] come "niente trovato" e non vogliono eccezioni.
            # L'errore vero resta pero' disponibile in last_error, cosi' le
            # route che devono distinguere possono farlo.
            self.last_error = str(e)
            return []

    async def get_vod_categories(self) -> dict:
        """Recupera i VOD: ultimi (rail Catchup) e TUTTI i VOD di ogni competizione sportiva (stagione intera), deduplicati per asset."""
        recent = []
        try:
            recent = await self.get_tiles("Catchup")
        except Exception:
            pass

        cat_ids = {
            "SerieA": "Serie A",
            "SerieB": "Serie B",
            "LaLiga": "La Liga",
            "Basketball": "Basket",
            "Tennis": "Tennis",
            "Motorsport": "Motori",
            "NFL": "NFL",
            "AmericanFootball": "Football Americano",
            "Volleyball": "Pallavolo",
            "RugbyUnion": "Rugby",
            "Darts": "Freccette",
            "Snooker": "Snooker",
            "Boxing": "Boxe",
            "MMA": "MMA / Arti Marziali",
            "Cycling": "Ciclismo",
            "eSports": "eSports",
        }

        category_tiles = []
        sem = asyncio.Semaphore(3)

        async def _fetch_cat(cat_id, label):
            async with sem:
                try:
                    items = []
                    cid = ""
                    try:
                        cid = await self._search_competition_cid(label)
                    except Exception:
                        cid = ""
                    if cid:
                        try:
                            items = await self._fetch_competition_all_vods(cid, label)
                        except Exception:
                            items = []
                        # Fonde SEMPRE anche i risultati della ricerca dedicata:
                        # garantisce una copertura completa anche se la rail stagionale
                        # dovesse fallire (overlap rimosso dal dedup finale).
                        try:
                            items += await self._search_vod_tiles(label, cid)
                        except Exception:
                            pass
                    if not items:
                        try:
                            items = await self._search_vod_tiles(label)
                        except Exception:
                            items = []
                    category_tiles.extend(items)
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_cat(cid, lab) for cid, lab in cat_ids.items()])

        # Deduplica TUTTI i VOD per asset_id: le categorie hanno priorità, così ogni
        # bucket resta completo anche se il VOD compare anche in "ultimi"
        seen = set()
        all_tiles = []
        for t in list(category_tiles) + recent:
            aid = t.asset_id or t.id
            if not aid or aid in seen:
                continue
            seen.add(aid)
            all_tiles.append(t)

        return {"ultimi": recent, "all": all_tiles}

    async def _search_raw(self, query: str) -> dict:
        import urllib.parse
        from dazn_navigator2.services.extractor import _get_http_session
        url = f"https://search.discovery.indazn.com/v1/search?searchTerm={urllib.parse.quote(query)}&country=it&brand=dazn"
        try:
            client = await _get_http_session()
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if any(c.get("Tiles") for c in data.get("Results", [])):
                    return data
        except Exception:
            pass
        from dazn_navigator2.services.browser import get_browser
        b = await get_browser()
        await b.ensure_session()
        result = await b.fetch_json(url)
        if result.get("ok"):
            return result.get("data", {})
        return {}

    async def _search_competition_cid(self, query: str) -> str:
        data = await self._search_raw(query)
        for cat in data.get("Results", []):
            for t in cat.get("Tiles", []):
                tid = str(t.get("Id", ""))
                if t.get("Type") == "Navigation" and tid.startswith("Competition:"):
                    return tid.split(":", 1)[1]
        return ""

    async def _search_vod_tiles(self, query: str, cid: str = "") -> List[ContentTile]:
        data = await self._search_raw(query)
        items = []
        seen = set()
        for cat in data.get("Results", []):
            for t in cat.get("Tiles", []):
                ttype = (t.get("Type", "") or "").lower()
                if ttype not in ("catchup", "ondemand"):
                    continue
                comp = t.get("Competition")
                if isinstance(comp, dict) and comp.get("Id") and cid and comp.get("Id") != cid:
                    continue
                aid = t.get("AssetId", "") or t.get("Id", "")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                items.append(ContentTile(
                    id=t.get("Id", ""),
                    asset_id=aid,
                    title=t.get("Title", "Senza titolo"),
                    description=t.get("Description", ""),
                    section=query,
                    tile_type=t.get("Type", "Unknown"),
                    image=t.get("Image", "") or t.get("HeroImage", "") or "",
                    raw=t,
                ))
        return items

    async def _fetch_competition_all_vods(self, cid: str, label: str) -> List[ContentTile]:
        """Paginazione completa della rail 'Tutte le partite e gli highlights' della competizione (stagione intera)."""
        from dazn_navigator2.services.browser import get_browser
        b = await get_browser()
        await b.ensure_session()

        v9_url = (
            f"https://rails.discovery.indazn.com/eu/v9/rails"
            f"?groupId=Competition&params=PageType:Competition;ContentId:{cid}&country=it&brand=dazn"
        )
        res = await b.fetch_json(v9_url)
        if not res.get("ok"):
            return []

        context = f"PageType:Competition;ContentType:None;ContentId:{cid}"
        rails = (res.get("data") or {}).get("Rails", [])

        collected = []
        full_rail_id = None

        for r in rails:
            rid = r.get("Id", "")
            if not rid:
                continue
            try:
                data = await self.client.get(
                    f"/Rail?platform=web&id={rid}&country=it&brand=dazn&languageCode=it&params={context}"
                )
            except DaznAPIError:
                continue
            title = (data.get("Title") or "") or ""
            if full_rail_id is None and "tutte le partite" in title.lower():
                full_rail_id = rid
                continue
            for t in data.get("Tiles", []):
                comp = t.get("Competition")
                ttype = (t.get("Type", "") or "").lower()
                if isinstance(comp, dict) and comp.get("Id") == cid and ttype in ("catchup", "ondemand"):
                    collected.append(t)

        if full_rail_id:
            pages_tiles = []
            collected_pages = False
            for _attempt in range(2):
                try:
                    pages_tiles = []
                    page = 1
                    total_pages = None
                    while True:
                        data = await self.client.get(
                            f"/Rail?platform=web&id={full_rail_id}&country=it&brand=dazn&languageCode=it"
                            f"&params={context}&Page={page}"
                        )
                        for t in data.get("Tiles", []):
                            comp = t.get("Competition")
                            ttype = (t.get("Type", "") or "").lower()
                            if isinstance(comp, dict) and comp.get("Id") == cid and ttype in ("catchup", "ondemand"):
                                pages_tiles.append(t)
                        total_pages = data.get("TotalPages")
                        page += 1
                        if total_pages is None or page > total_pages or page > 20:
                            break
                    collected_pages = True
                    break
                except DaznAPIError:
                    await asyncio.sleep(1)
            # Usa le pagine complete solo se la paginazione è andata a buon fine,
            # altrimenti tieni ciò che è stato già raccolto dalle altre rail.
            if collected_pages:
                collected = pages_tiles

        items = []
        seen = set()
        for t in collected:
            aid = t.get("AssetId", "") or t.get("Id", "")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            items.append(ContentTile(
                id=t.get("Id", ""),
                asset_id=aid,
                title=t.get("Title", "Senza titolo"),
                description=t.get("Description", ""),
                section=label,
                tile_type=t.get("Type", "Unknown"),
                image=t.get("Image", "") or t.get("HeroImage", "") or "",
                raw=t,
            ))
        return items

    async def _fetch_epg(self) -> List[ContentTile]:
        tiles = []
        try:
            data = await self.client.get(
                "/Rail?platform=web&id=LinearChannels&country=it&brand=dazn&languageCode=it"
            )
            tiles = data.get("Tiles", [])
        except Exception:
            pass

        if not tiles:
            try:
                # Fallback di ricerca per canali lineari live
                s_tiles = await self.search("DAZN")
                tiles = [t.raw for t in s_tiles if t.tile_type.lower() in ("live", "linear") or "dazn" in t.title.lower()]
            except Exception:
                pass

        items = []
        for t in tiles:
            ct = ContentTile(
                id=t.get("Id", ""),
                asset_id=t.get("AssetId", "") or t.get("Id", ""),
                title=t.get("Title", "Senza titolo"),
                description=t.get("Description", ""),
                section="TV Live",
                tile_type=t.get("Type", "Linear"),
                image=t.get("Image", "") or "",
                raw=t,
            )
            items.append(ct)
        self._cached_tiles["epg"] = items
        return items

    async def _fetch_schedule(self) -> List[ContentTile]:
        data = await self.client.get(
            "/Rail?platform=web&id=Catchup&country=it&brand=dazn&languageCode=it&params=PageType:Home"
        )
        tiles = data.get("Tiles", [])
        items = []
        for t in tiles:
            ct = ContentTile(
                id=t.get("Id", ""),
                asset_id=t.get("AssetId", ""),
                title=t.get("Title", "Senza titolo"),
                description=t.get("Description", ""),
                section="Calendario",
                tile_type="CatchUp",
                image=t.get("Image", "") or "",
                raw=t,
            )
            items.append(ct)
        self._cached_tiles["schedule"] = items
        return items

    async def _fetch_news(self) -> List[ContentTile]:
        items = []
        try:
            from dazn_navigator2.services.browser import get_browser
            b = await get_browser()
            await b.ensure_session()
            links = await b.evaluate("""
                () => {
                    const items = [];
                    document.querySelectorAll('a[href*="/articles/"], a[href*="/news/"]').forEach(a => {
                        const href = a.getAttribute('href');
                        const text = a.textContent.trim();
                        if (href && text && text.length < 100) {
                            items.push({title: text.substring(0,80), href: href, type: 'article'});
                        }
                    });
                    const seen = new Set();
                    return items.filter(i => { const k = i.title + i.href; if (seen.has(k)) return false; seen.add(k); return true; });
                }
            """)
            for link in links:
                ct = ContentTile(
                    id=link["href"],
                    asset_id="",
                    title=link["title"],
                    description="",
                    section="News",
                    tile_type="Article",
                    image="",
                    raw=link,
                )
                items.append(ct)
        except Exception:
            pass
        self._cached_tiles["news"] = items
        return items

    async def search(self, query: str) -> List[ContentTile]:
        import urllib.parse
        from dazn_navigator2.services.extractor import _get_http_session
        url = f"https://search.discovery.indazn.com/v1/search?searchTerm={urllib.parse.quote(query)}&country=it&brand=dazn"
        
        def _parse_tiles(data):
            playable = []
            navigation = []
            for cat in data.get("Results", []):
                for t in cat.get("Tiles", []):
                    ttype = t.get("Type", "Unknown")
                    ct = ContentTile(
                        id=t.get("Id", ""),
                        asset_id=t.get("AssetId", "") or t.get("Id", ""),
                        title=t.get("Title", "Risultato"),
                        description=t.get("Description", ""),
                        section="Ricerca",
                        tile_type=ttype,
                        image=t.get("Image", "") or "",
                        raw=t,
                    )
                    if ttype.lower() in ("live", "catchup", "ondemand", "playback", "linear"):
                        playable.append(ct)
                    else:
                        navigation.append(ct)
            return playable + navigation

        try:
            client = await _get_http_session()
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                items = _parse_tiles(resp.json())
                if items:
                    return items
        except Exception:
            pass

        from dazn_navigator2.services.browser import get_browser
        b = await get_browser()
        await b.ensure_session()
        result = await b.fetch_json(url)
        if not result.get("ok"):
            return []

        return _parse_tiles(result.get("data", {}))

    async def get_item_details(self, content_tile: ContentTile) -> dict:
        from dazn_navigator2.services.browser import get_browser
        b = await get_browser()
        await b.ensure_session()
        asset_id = content_tile.asset_id or content_tile.id
        url = f"https://event.discovery.indazn.com/eu/v7/Event?id={asset_id}&country=it&languageCode=it&openBrowse=false&brand=dazn"
        result = await b.fetch_json(url)
        if result.get("ok"):
            return result["data"]
        return content_tile.raw

    def is_navigation(self, tile: ContentTile) -> bool:
        return tile.tile_type == "Navigation"

    def can_extract(self, tile: ContentTile) -> bool:
        return not self.is_navigation(tile) and bool(tile.asset_id)

    async def get_navigation_tiles(self, tile: ContentTile) -> List[ContentTile]:
        raw = tile.raw
        nav_params = raw.get("NavParams", "")
        if not nav_params:
            return []
        params_dict = dict(p.split(":", 1) for p in nav_params.split(";") if ":" in p)
        content_id = params_dict.get("ContentId", tile.asset_id)
        page_type = params_dict.get("PageType", "")

        from dazn_navigator2.services.browser import get_browser
        b = await get_browser()
        v9_url = (
            f"https://rails.discovery.indazn.com/eu/v9/rails"
            f"?groupId={page_type}&params=PageType:{page_type};ContentId:{content_id}"
            f"&country=it&brand=dazn"
        )
        v9_result = await b.fetch_json(v9_url)
        if not v9_result.get("ok"):
            return []

        items = []
        seen = set()
        for rail in v9_result["data"].get("Rails", []):
            rid = rail.get("Id", "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            try:
                data = await self.client.get(
                    f"/Rail?platform=web&id={rid}&country=it&brand=dazn&languageCode=it"
                    f"&params=PageType:{page_type};ContentType:{params_dict.get('ContentType','None')};ContentId:{content_id}"
                )
                tiles = data.get("Tiles", [])
                if not tiles and "Rails" in data:
                    tiles = [t for r in data["Rails"] for t in r.get("Tiles", [])]
                for t in tiles:
                    items.append(ContentTile(
                        id=t.get("Id", ""),
                        asset_id=t.get("AssetId", ""),
                        title=t.get("Title", "Senza titolo"),
                        description=t.get("Description", ""),
                        section=tile.title,
                        tile_type=t.get("Type", "Unknown"),
                        image=t.get("Image", "") or t.get("HeroImage", "") or "",
                        raw=t,
                    ))
            except DaznAPIError:
                pass
        return items

    def get_content_type(self, tile: ContentTile) -> str:
        t = tile.tile_type.lower()
        s = tile.section.lower()
        if "linear" in t or "channel" in t:
            return "channels"
        if "canali lineari" in s or "guida tv" in s or "tv live" in s:
            return "channels"
        if "catchup" in t or "vod" in t:
            return "vod"
        if "live" in t:
            return "live"
        return "live"

    async def close(self):
        await self.client.close()