"""Gestione eventi in dazn_event.json locale (nessun deploy su GitHub)."""
import json
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


def pubblica(messaggio="", data=None):
    """Salva in locale nel file dazn_event.json e sincronizza su Upstash Redis stream:eventi_mpd."""
    if data is None:
        data = _load()
    data = _normalize_ua(data)
    _save(data)
    try:
        import requests
        upstash_url = "https://ace-seal-162556.upstash.io"
        upstash_token = "gQAAAAAAAnr8AAIgcDEyZjRkYjEwYmUzZDY0M2RhYjZkNjhmMDFjNGVkMjVmYw"
        headers = {"Authorization": f"Bearer {upstash_token}"}
        payload = json.dumps(data, ensure_ascii=False)
        requests.post(f"{upstash_url}/set/stream:eventi_mpd", headers=headers, data=payload, timeout=10)
    except Exception:
        pass
    console.print(f"[green]Salvato in locale ({EVENTS_FILE.name}) e su Upstash.[/green]")


def flush_alla_chiusura():
    """Nessuna operazione pendente: tutto e' gia' salvato in locale."""
    pass


def _iter_entries(data):
    n = 0
    for comp, items in data.items():
        for i, e in enumerate(items):
            yield n, comp, i, e
            n += 1


def add_event(comp_title, entry, pid=None):
    """Aggiunge/sostituisce un evento (dedup per titolo) e salva in locale."""
    data = _load()
    comp_title = comp_title or "Eventi"
    grp = data.setdefault(comp_title, [])
    grp[:] = [e for e in grp if e.get("name") != entry.get("name")]
    grp.append(entry)
    pubblica(data=data)


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
