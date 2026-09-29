"""Gestione eventi in dazn_event.json locale (nessun deploy su GitHub)."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt

EVENTS_FILE = Path(__file__).resolve().parent.parent.parent / "dazn_event.json"
console = Console()


def get_events_file():
    return EVENTS_FILE


def _load(pid=None):
    if EVENTS_FILE.exists():
        try:
            data = json.loads(EVENTS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            console.print(f"[red]dazn_event.json corrotto ({e}) - backup in dazn_event.json.bak[/red]")
            try:
                bak = EVENTS_FILE.with_suffix(".json.bak")
                bak.write_text(EVENTS_FILE.read_text(encoding="utf-8-sig"), encoding="utf-8")
            except Exception:
                pass
    return {}


def _save(data, pid=None):
    """Scrive il file locale dazn_event.json."""
    EVENTS_FILE.write_text(json.dumps(data, indent=3, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize_ua(data):
    """Garantisce che ogni evento pubblicato abbia un User-Agent valido.

    Il token CDN DAZN e' vincolato all'UA con cui e' stato creato: se l'addon
    riceve un 'ua' vuoto (o un UA non di browser) la CDN risponde 401 e il
    manifest/segmenti non si aprono. Qui si compila solo quando manca, senza
    toccare gli eventi gia' corretti.
    """
    try:
        from dazn_navigator2.services.extractor import detect_user_agent, _valid_browser_ua
        default_ua = detect_user_agent()
    except Exception:
        return data
    for items in data.values():
        if not isinstance(items, list):
            continue
        for ev in items:
            if not isinstance(ev, dict):
                continue
            if not ev.get("mpd") and not ev.get("manifest"):
                continue
            if not _valid_browser_ua(ev.get("ua") or ""):
                ev["ua"] = default_ua
    return data


_push_lock = threading.Lock()
_push_pending = None
_push_thread = None


def _upstash_set(payload):
    """Scrive lo stato su Upstash. Ritorna True/False, non alza mai."""
    try:
        import requests
        upstash_url = "https://ace-seal-162556.upstash.io"
        upstash_token = "gQAAAAAAAnr8AAIgcDEyZjRkYjEwYmUzZDY0M2RhYjZkNjhmMDFjNGVkMjVmYw"
        headers = {"Authorization": f"Bearer {upstash_token}"}
        requests.post(f"{upstash_url}/set/stream:eventi_mpd", headers=headers,
                      data=payload, timeout=10)
        return True
    except Exception:
        return False


def _push_worker():
    global _push_pending
    while True:
        with _push_lock:
            payload = _push_pending
            _push_pending = None
        if payload is None:
            return
        _upstash_set(payload)


def _push_async(payload):
    """Mette in coda l'ultimo stato senza bloccare chi chiama.

    La chiave Upstash e' un overwrite completo, quindi gli stati intermedi
    sono superflui: si tiene solo l'ultimo payload e si manda quello.
    """
    global _push_pending, _push_thread
    with _push_lock:
        _push_pending = payload
        if _push_thread is None or not _push_thread.is_alive():
            _push_thread = threading.Thread(target=_push_worker, daemon=True)
            _push_thread.start()


def pubblica(messaggio="", data=None, asincrono=False):
    """Salva in locale nel file dazn_event.json e sincronizza su Upstash Redis stream:eventi_mpd.

    asincrono=True (per il sito) rimanda il push a Upstash in background: il
    click su "Estrai" non aspetta i ~2.5s della rete. Il file locale resta
    scritto in modo sincrono, quindi la lista e' subito aggiornata.
    """
    if data is None:
        data = _load()
    data = _normalize_ua(data)
    _save(data)
    payload = json.dumps(data, ensure_ascii=False)
    if asincrono:
        _push_async(payload)
        console.print(f"[green]Salvato in locale ({EVENTS_FILE.name}); push Upstash in corso.[/green]")
        return
    _upstash_set(payload)
    console.print(f"[green]Salvato in locale ({EVENTS_FILE.name}) e su Upstash.[/green]")


def flush_alla_chiusura():
    """Attende l'eventuale push a Upstash ancora in coda."""
    t = _push_thread
    if t is not None and t.is_alive():
        t.join(timeout=5)


def _iter_entries(data):
    n = 0
    for comp, items in data.items():
        for i, e in enumerate(items):
            yield n, comp, i, e
            n += 1


# ---------------------------------------------------------------------------
# Pool eventi: unisce le due fonti che il menu Pz8 (tasti 3 e 4) invia su
# Upstash. Le due fonti descrivono gli STESSI eventi in due formati diversi,
# quindi non sono da fondere per origine ma da normalizzare e deduplicare.
# ---------------------------------------------------------------------------

def is_mpd(url):
    """True solo se l'URL e' un manifest MPEG-DASH (.mpd).

    Esclude m3u8/HLS, link senza estensione e URL vuoti: il sito li mostrava
    insieme ai DASH e i canali non MPD non si aprono in Kodi.
    """
    if not url:
        return False
    u = str(url).split("?", 1)[0].split("#", 1)[0].strip().lower()
    return u.endswith(".mpd")


def _split_sportzx_link(link):
    """'https://x.cenc.mpd|user-agent=Mozilla/5.0 ...' -> (mpd_url, user_agent)."""
    if not link:
        return "", ""
    text = str(link)
    if "|" in text:
        url, _, tail = text.partition("|")
        ua = tail.strip()
        if ua.lower().startswith("user-agent="):
            ua = ua[len("user-agent="):]
        return url.strip(), ua.strip()
    return text.strip(), ""


def _upstash_get(key):
    """Legge una chiave da Upstash. Ritorna None se irraggiungibile o assente."""
    try:
        import requests
        url = f"https://ace-seal-162556.upstash.io/get/stream:{key}"
        headers = {"Authorization": "Bearer gQAAAAAAAnr8AAIgcDEyZjRkYjEwYmUzZDY0M2RhYjZkNjhmMDFjNGVkMjVmYw"}
        res = requests.get(url, headers=headers, timeout=10)
        if not res.ok:
            return None
        raw = res.json().get("result")
        if not raw or raw == "null":
            return None
        return json.loads(raw)
    except Exception:
        return None


def _norm_channel(name, mpd, kid, ua):
    """Canale normalizzato, o None se non e' MPD."""
    mpd = (mpd or "").strip()
    if not is_mpd(mpd):
        return None
    return {
        "name": (name or "").strip() or "Senza nome",
        "mpd": mpd,
        "key": (kid or "").strip(),
        "ua": (ua or "").strip(),
    }


def _norm_time(value):
    """Rende l'orario confrontabile: '2026/09/26 10:55:00 +0000' -> ISO."""
    if not value:
        return ""
    s = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return s


def build_event_pool():
    """Unisce sportzx_cached e fctv33_cached: 1 evento con tutti i suoi canali MPD.

    Ritorna (pool, stats). Ogni voce del pool:
      {id, title, category, start, image, sources, channels: [...]}
    I canali sono deduplicati per kid e, in assenza, per URL del manifest.
    """
    pool = {}
    stats = {"fctv33": 0, "sportzx": 0, "canali": 0, "scartati_non_mpd": 0}

    def touch(eid, title, category, start, image, source):
        key = eid if eid is not None else f"{_norm_time(start)}|{(title or '').strip().lower()}"
        item = pool.get(key)
        if item is None:
            item = {"id": eid, "title": (title or "").strip() or "Senza titolo",
                    "category": (category or "").strip() or "Eventi",
                    "start": start, "image": image, "sources": [], "channels": []}
            pool[key] = item
        if source not in item["sources"]:
            item["sources"].append(source)
        if not item.get("image") and image:
            item["image"] = image
        if not item.get("start") and start:
            item["start"] = start
        return item

    def add_channel(item, ch):
        if ch is None:
            stats["scartati_non_mpd"] += 1
            return
        for c in item["channels"]:
            if ch["key"] and ch["key"] == c["key"]:
                if not c["ua"] and ch["ua"]:
                    c["ua"] = ch["ua"]
                return
            if not ch["key"] and ch["mpd"] == c["mpd"]:
                return
        item["channels"].append(ch)
        stats["canali"] += 1

    # Fonte 4: fctv33, gia' nel formato finale (channels[].mpd_url)
    fct = _upstash_get("fctv33_cached") or {}
    for ev in (fct.get("events") or []):
        if not isinstance(ev, dict):
            continue
        stats["fctv33"] += 1
        item = touch(ev.get("id"), ev.get("event_title"), ev.get("category"),
                     _norm_time(ev.get("startTime")), "", "fctv33")
        for c in (ev.get("channels") or []):
            if not isinstance(c, dict):
                continue
            add_channel(item, _norm_channel(c.get("name"), c.get("mpd_url"),
                                            c.get("kid_key"), c.get("user_agent")))

    # Fonte 3: sportzx, formato diverso (decoded_channels[].link con |user-agent=)
    spz = _upstash_get("sportzx_cached") or []
    if isinstance(spz, dict):
        spz = spz.get("events") or []
    for ev in (spz or []):
        if not isinstance(ev, dict):
            continue
        stats["sportzx"] += 1
        info = ev.get("eventInfo") or {}
        item = touch(ev.get("id"), ev.get("title") or info.get("eventName"), ev.get("cat"),
                     _norm_time(info.get("startTime")), info.get("eventBanner"), "sportzx")
        for c in (ev.get("decoded_channels") or []):
            if not isinstance(c, dict):
                continue
            mpd, ua = _split_sportzx_link(c.get("link"))
            add_channel(item, _norm_channel(c.get("title"), mpd, c.get("api"), ua))

    out = [v for v in pool.values() if v["channels"]]
    out.sort(key=lambda e: (e.get("start") or "9999"))
    return out, stats


def pool_entry(event, channel):
    """Costruisce l'entry da salvare in eventi estratti per il canale scelto.

    Restituisce None se il canale non e' MPD: cosi' l'endpoint non puo' essere
    usato per infilare un m3u8/HLS nell'archivio.
    """
    mpd = (channel.get("mpd") or "").strip()
    if not is_mpd(mpd):
        return None
    return {
        "name": (event.get("title") or "").strip() or "Senza titolo",
        "image": event.get("image") or "",
        "start": event.get("start") or "",
        "end": event.get("end") or "",
        "mpd": mpd,
        "key": (channel.get("key") or "").strip(),
        "ua": (channel.get("ua") or "").strip(),
    }


def add_event(comp_title, entry, pid=None, asincrono=False):
    """Aggiunge/sostituisce un evento (dedup per titolo) e salva in locale."""
    data = _load()
    comp_title = comp_title or "Eventi"
    grp = data.setdefault(comp_title, [])
    grp[:] = [e for e in grp if e.get("name") != entry.get("name")]
    grp.append(entry)
    pubblica(data=data, asincrono=asincrono)


def ripara_user_agent(data=None, pubblica_risultato=True):
    """Rilegge ogni evento e riapplica l'User-Agent accettato dalla CDN DAZN.

    Gli eventi estratti con un profilo TLS diverso (o senza 'ua') hanno un
    token CDN che la CDN rifiuta: si riprovano i candidati e si scrive quello
    che restituisce HTTP 200.

    Ritorna (data, report) dove ogni voce e' (comp, nome, ua_precedente,
    ua_verificata, cambiato). events_cmds 'fixed' mostra solo quelli corretti.
    """
    from dazn_navigator2.services.extractor import validate_cdn_user_agent, ua_candidates
    if data is None:
        data = _load()
    report = []
    candidates = ua_candidates()
    for _, comp, i, ev in _iter_entries(data):
        mpd = ev.get("mpd") or ev.get("manifest") or ""
        if not mpd:
            continue
        current = (ev.get("ua") or "").strip()
        found = validate_cdn_user_agent(mpd, ev.get("dazn_token") or "", [current] + candidates)
        changed = bool(found) and found != current
        if changed:
            ev["ua"] = found
        report.append((comp, ev.get("name", "?"), current, found, changed))
    if any(r[4] for r in report) and pubblica_risultato:
        pubblica(data=data)
    return data, report


def _sort_key(item):
    _, _, _, ev = item
    s = (ev.get("start") or "").strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


def _fetch_from_upstash():
    """Ritorna i dati direttamente dal file locale dazn_event.json."""
    return _load()


def manage_events():
    while True:
        data = _fetch_from_upstash()
        entries = list(_iter_entries(data))
        console.print("\n[bold magenta]=== EVENTI (API Upstash) ===[/bold magenta]")
        if not entries:
            console.print("[yellow]Nessun evento presente nell'API.[/yellow]")
        else:
            for n, comp, i, e in entries:
                console.print(f"[cyan]{n + 1:2d}.[/cyan] [{comp}] {e.get('name', '?')}")
        
        console.print("""
[bold cyan]1.[/bold cyan] Modifica Titolo
[bold cyan]2.[/bold cyan] Elimina Eventi (es. '1', '1 3 4', o 'tutti')
[bold cyan]0.[/bold cyan] Indietro""")
        
        scelta = Prompt.ask("Scelta", default="0").strip()
        if scelta == "0":
            break
        elif scelta == "1":
            if not entries:
                console.print("[yellow]Nessun evento da modificare.[/yellow]")
                continue
            idx = Prompt.ask("Numero evento da modificare", default="")
            if not idx.isdigit() or not (1 <= int(idx) <= len(entries)):
                console.print("[red]Numero non valido[/red]")
                continue
            n, comp, i, e = entries[int(idx) - 1]
            nuovo = Prompt.ask("Nuovo titolo", default=e.get("name", ""))
            if nuovo.strip():
                e["name"] = nuovo.strip()
                pubblica(data=data)
                console.print("[green]Titolo aggiornato su API e locale.[/green]")
        elif scelta == "2":
            if not entries:
                console.print("[yellow]Nessun evento da eliminare.[/yellow]")
                continue
            q = Prompt.ask("Numeri da eliminare (es. '1 3 5') o 'tutti'", default="")
            ql = q.strip().lower()
            if ql == "tutti":
                pubblica(data={})
                console.print("[green]Tutti gli eventi eliminati dall'API e in locale.[/green]")
                continue
            to_del = set()
            for p in ql.split():
                if p.isdigit() and 1 <= int(p) <= len(entries):
                    to_del.add(int(p))
            if not to_del:
                console.print("[red]Nessun numero valido[/red]")
                continue
            for k in sorted(to_del, reverse=True):
                n, comp, i, e = entries[k - 1]
                del data[comp][i]
                if not data[comp]:
                    del data[comp]
            pubblica(data=data)
            console.print(f"[green]{len(to_del)} evento/i eliminato/i dall'API e in locale.[/green]")
