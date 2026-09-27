import typer, asyncio, urllib.parse
from typing import List, Optional
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from dazn_navigator2.services.explorer import DaznExplorer, ContentTile
from dazn_navigator2.auth.token_refresh import PROFILE_DIR
from dazn_navigator2.services.extractor import HeadlessExtractor
from dazn_navigator2.utils.formatters import print_events_table, export_to_json, export_to_csv

app = typer.Typer(help="Naviga contenuti DAZN.")
console = Console()


def run_async(coro):
    return asyncio.run(coro)


async def _show_section_menu(explorer: DaznExplorer):
    while True:
        console.print("\n[bold magenta]=== NAVIGAZIONE COMPLETA DAZN ===[/bold magenta]")
        
        menu = """[bold cyan]1.[/bold cyan] Home Page
[bold cyan]2.[/bold cyan] Eventi Live
[bold cyan]3.[/bold cyan] Contenuti VOD (On-Demand)
[bold cyan]4.[/bold cyan] Canali Lineari (DAZN TV)
[bold cyan]5.[/bold cyan] Cerca un Evento
[bold cyan]0.[/bold cyan] Indietro"""
        
        console.print(menu)

        choice = Prompt.ask("\nScegli un'opzione", default="0").strip().upper()

        if choice == "0":
            break
        elif choice == "1":
            await _show_tiles(explorer, "Home", "Home Page")
        elif choice == "2":
            await _show_tiles(explorer, "Live", "Eventi Live")
        elif choice == "3":
            await _show_tiles(explorer, "Catchup", "Contenuti VOD (On-Demand)")
        elif choice == "4":
            await _show_tiles(explorer, "epg", "Canali Lineari (DAZN TV)")
        elif choice == "5" or choice == "C":
            await _search_menu(explorer)
        else:
            console.print("[red]Scelta non valida[/red]")

async def _show_tiles(explorer: DaznExplorer, section_id: str, section_title: str, tiles: List[ContentTile] = None):
    if tiles is None:
        console.print(f"\n[bold cyan]Caricamento: {section_title}...[/bold cyan]")
        tiles = await explorer.get_tiles(section_id)

    if not tiles:
        console.print(f"[yellow]Nessun contenuto trovato in '{section_title}'.[/yellow]")
        return

    page_size = 20
    current_page = 0
    total_pages = (len(tiles) + page_size - 1) // page_size

    while True:
        start = current_page * page_size
        end = min(start + page_size, len(tiles))
        page_tiles = tiles[start:end]

        title = section_title if total_pages <= 1 else f"{section_title} (Pagina {current_page + 1}/{total_pages})"
        n = len(page_tiles)
        console.print(f"\n[bold]{title}[/bold] - [green]{n} contenuti[/green]")

        is_linear = section_id in ("LinearChannels", "epg") or "Canali Lineari" in section_title
        is_nav = section_id == "Sport" or "Tutti gli Sport" in section_title or all(t.tile_type == "Navigation" for t in page_tiles)

        tile_table = Table(show_header=True, header_style="bold cyan")
        if is_nav:
            tile_table.add_column("#", justify="right", style="cyan")
            tile_table.add_column("Sezione", style="white")
            tile_table.add_column("Tipo", style="yellow")
        elif is_linear:
            tile_table.add_column("#", justify="right", style="cyan")
            tile_table.add_column("Canale", style="white")
            tile_table.add_column("Tipo", style="yellow")
        else:
            tile_table.add_column("#", justify="right", style="cyan")
            tile_table.add_column("Titolo", style="white")
            tile_table.add_column("Data/Ora", style="green")
            tile_table.add_column("Tipo", style="yellow")
            tile_table.add_column("Sport", style="blue")
            tile_table.add_column("Competizione", style="magenta")

        for i, t in enumerate(page_tiles, start + 1):
            raw = t.raw
            if is_nav:
                tile_table.add_row(str(i), t.title, "Sezione")
            elif is_linear:
                tile_table.add_row(str(i), t.title, "Live TV")
            else:
                start_raw = raw.get("Start") or raw.get("DisplayDate") or ""
                ora = _fmt_date(start_raw)
                sport = raw.get("Sport", {})
                if isinstance(sport, dict):
                    sport = sport.get("Title", "")
                comp = raw.get("Competition", {})
                if isinstance(comp, dict):
                    comp = comp.get("Title", "")
                tile_table.add_row(str(i), t.title, ora, t.tile_type, str(sport), str(comp))

        console.print(tile_table)

        console.print("\n[dim]'D' per dettagli, o:[/dim]")
        nav_hint = ""
        if current_page > 0:
            nav_hint += " 'P' indietro"
        if current_page < total_pages - 1:
            nav_hint += " 'N' avanti"
        console.print(f"[dim]{nav_hint} - Invio per tornare alle sezioni[/dim]")

        choice = Prompt.ask("Scelta", default="").strip().upper()

        if not choice:
            break
        if choice == "N" and current_page < total_pages - 1:
            current_page += 1
            continue
        if choice == "P" and current_page > 0:
            current_page -= 1
            continue
        if choice == "D":
            idx = Prompt.ask("Numero del tile per dettagli", default="")
            if idx.isdigit():
                i = int(idx) - 1
                if 0 <= i < len(tiles):
                    details = await explorer.get_item_details(tiles[i])
                    console.print(f"\n[bold cyan]Dettagli:[/bold cyan] {tiles[i].title}")
                    console.print(f"  ID: {details.get('Id', 'N/A')}")
                    console.print(f"  Sport: {details.get('Sport', {}).get('Title', 'N/A')}")
                    console.print(f"  Competizione: {details.get('Competition', {}).get('Title', 'N/A')}")
                    console.print(f"  Inizio: {details.get('Start', 'N/A')}")
                    console.print(f"  Durata: {details.get('Duration', 'N/A')} minuti")
            continue

        indices = []
        for p in choice.split():
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(tiles):
                    indices.append(idx)

        if not indices:
            continue

        await _handle_selected(explorer, tiles, indices)


async def _handle_selected(explorer: DaznExplorer, tiles: list, indices: list):
    risultati = []
    for idx in indices:
        tile = tiles[idx]
        if explorer.is_navigation(tile):
            console.print(f"\n[cyan]Esploro: {tile.title}...[/cyan]")
            subtiles = await explorer.get_navigation_tiles(tile)
            if subtiles:
                await _show_tiles(explorer, tile.id, tile.title, subtiles)
            else:
                console.print(f"[yellow]Nessun contenuto in '{tile.title}'.[/yellow]")
            continue
        asset_id = tile.asset_id or tile.id
        try:
            s_res = await explorer.search(tile.title)
            match = [x for x in s_res if x.title.strip().lower() == tile.title.strip().lower()]
            if match and match[0].asset_id:
                asset_id = match[0].asset_id or match[0].id
        except Exception:
            pass

        console.print(f"\n[cyan]Estrazione: {tile.title}...[/cyan]")
        ext = HeadlessExtractor()
        try:
            result = await asyncio.wait_for(ext.estrai(str(PROFILE_DIR), asset_id, tile.title), timeout=35.0)
        except asyncio.TimeoutError:
            console.print("[bold red]  ✗ Timeout durante l'estrazione: sessione/token DAZN non responsive o scaduto.[/bold red]")
            result = {"ok": False, "error": "Timeout (35s) durante l'estrazione"}
        info = _build_entry(tile, result)
        if info:
            risultati.append(info)
    if risultati:
        _print_batch(risultati)
        from dazn_navigator2.cli.eventi_cmds import pubblica
        pubblica("dazn2: estrazione %s" % ", ".join(e['name'] for _, e, _, _ in risultati))


def _image_url(img) -> str:
    """Converte il campo image in URL discovery v3 identico al formato richiesto."""
    if isinstance(img, dict):
        img_id = img.get("Id", "")
        if img_id:
            return f"https://image.discovery.indazn.com/eu/v3/eu/none/{img_id}/fill/none/top/none/100/1280/720/png/image"
        return ""
    return str(img) if img else ""


def _build_entry(tile: ContentTile, result: dict):
    """Estrae i dati, salva l'evento e ritorna (comp, entry, scadenza) o None."""
    if not result.get("ok"):
        console.print(f"[bold red]Fallito: {tile.title} - {result.get('error', 'Errore sconosciuto')}[/bold red]")
        return None
    titolo = result.get('titolo', tile.title)
    logo = _image_url(tile.image)

    keys_str = ','.join(result.get('keys', []))
    dazn_token = result.get('jwt') or result.get('dazn_token', '')
    mpd_url = result['mpd_url']
    
    # Inserimento token nel path (@token/...) come richiesto per durata 24 ore
    if dazn_token:
        if "://" in mpd_url:
            proto, rest = mpd_url.split("://", 1)
            if "/" in rest:
                host, path = rest.split("/", 1)
                mpd_auth = f"{proto}://{host}/@{dazn_token}/{path}"
            else:
                mpd_auth = f"{proto}://{rest}/@{dazn_token}"
        else:
            mpd_auth = mpd_url
    else:
        mpd_auth = mpd_url

    raw = getattr(tile, 'raw', {}) or {}
    comp = raw.get('Competition', {})
    if isinstance(comp, dict):
        comp_title = comp.get('Title', '') or 'Eventi'
    else:
        comp_title = str(comp) or 'Eventi'
    
    from dazn_navigator2.settings import get_setting
    entry = {
        'name': titolo,
        'image': logo,
        'start': raw.get('Start') or '',
        'end': raw.get('End') or '',
        'mpd': mpd_auth,
        'key': keys_str,
    }
    # UA nel JSON: controllato separatamente per curl e headless
    engine = get_setting("extraction_engine")
    ua_key = "include_ua_headless" if engine == "headless" else "include_ua_curl"
    if result.get("ua") and get_setting(ua_key):
        entry['ua'] = result['ua']

    from dazn_navigator2.cli.eventi_cmds import add_event
    add_event(comp_title, entry)
    from dazn_navigator2.services.playlist_ed import token_expiry
    return comp_title, entry, token_expiry(dazn_token), result.get('ext_url', '')


def _print_batch(risultati):
    """Stampa un unico JSON con tutti gli eventi estratti + riepilogo scadenze."""
    import json as _json
    from datetime import datetime
    raggruppato = {}
    
    from dazn_navigator2.settings import get_setting
    for _, entry, _, ext_url in risultati:
        if ext_url and get_setting("print_extension_link"):
            console.print(f"\n[bold cyan]Link Estensione ({entry['name']}):[/bold cyan]\n[green]{ext_url}[/green]")
    console.print("")
    
    for comp, entry, _, _ in risultati:
        raggruppato.setdefault(comp, []).append(entry)
    console.print(_json.dumps(raggruppato, indent=3))
    
    for comp, entry, scadenza, _ in risultati:
        if scadenza is not None:
            scad_str = scadenza.strftime('%d/%m/%Y %H:%M')
            stato = '[green]✓[/green]' if scadenza > datetime.now() else '[red]SCADUTO[/red]'
        else:
            scad_str = 'non determinabile'
            stato = '[yellow]?[/yellow]'
        console.print("")
        console.print(f"[bold]{entry['name']}[/bold] {stato}")
        console.print(f"Scadenza link: {scad_str}")


def _fmt_date(d: str) -> str:
    if not d:
        return ""
    from datetime import datetime
    try:
        clean = d[:19].replace("T", " ")
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d/%m %H:%M")
    except:
        return d[:10]


async def _search_menu(explorer: DaznExplorer):
    query = Prompt.ask("\n[bold cyan]Cerca[/bold cyan] (lascia vuoto per tornare)")
    if not query:
        return

    console.print(f"[cyan]Cerco: '{query}'...[/cyan]")
    results = await explorer.search(query)

    if not results:
        console.print("[yellow]Nessun risultato.[/yellow]")
        return

    tiles = results
    page_size = 20
    current_page = 0
    total_pages = (len(tiles) + page_size - 1) // page_size

    while True:
        start = current_page * page_size
        end = min(start + page_size, len(tiles))
        page_tiles = tiles[start:end]

        console.print(f"\n[bold]Risultati per '{query}'[/bold] - [green]{len(tiles)} trovati[/green]")
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("#", justify="right", style="cyan", width=4)
        t.add_column("Titolo", style="white")
        t.add_column("Tipo", style="yellow")
        for i, tl in enumerate(page_tiles, start + 1):
            t.add_row(str(i), tl.title, tl.tile_type)
        console.print(t)

        nav = ""
        if current_page > 0:
            nav += " 'P' indietro"
        if current_page < total_pages - 1:
            nav += " 'N' avanti"
        console.print(f"[dim]{nav} - 'D' dettagli - Invio per tornare[/dim]")

        choice = Prompt.ask("Scelta", default="").strip().upper()
        if not choice:
            break
        if choice == "N" and current_page < total_pages - 1:
            current_page += 1
            continue
        if choice == "P" and current_page > 0:
            current_page -= 1
            continue
        if choice == "D":
            idx = Prompt.ask("Numero per dettagli", default="")
            if idx.isdigit():
                i = int(idx) - 1
                if 0 <= i < len(tiles):
                    details = await explorer.get_item_details(tiles[i])
                    console.print(f"\n[bold cyan]Dettagli:[/bold cyan] {tiles[i].title}")
                    console.print(f"  ID: {details.get('Id', 'N/A')}")
                    console.print(f"  Tipo: {details.get('Type', 'N/A')}")
            continue

        indices = []
        for p in choice.split():
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(tiles):
                    indices.append(idx)
        if indices:
            await _handle_selected(explorer, tiles, indices)


@app.command("naviga")
def naviga():
    """Esplora la home page e tutte le sezioni."""
    async def _run():
        explorer = DaznExplorer()
        try:
            await _show_section_menu(explorer)
        finally:
            await explorer.close()
    run_async(_run())


@app.command("cerca")
def search(query: str = typer.Argument("", help="Testo da cercare")):
    """Cerca contenuti su DAZN."""
    async def _run():
        explorer = DaznExplorer()
        try:
            if query:
                tiles = await explorer.search(query)
                if tiles:
                    await _show_tiles(explorer, "search", f"Risultati per '{query}'", tiles)
                else:
                    console.print("[yellow]Nessun risultato.[/yellow]")
            else:
                await _show_section_menu(explorer)
        finally:
            await explorer.close()
    run_async(_run())


async def navigate():
    """Avvia il navigatore completo (usato da main menu)."""
    explorer = DaznExplorer()
    try:
        await _show_section_menu(explorer)
    finally:
        await explorer.close()

async def quick_navigate(section_id: str, section_title: str):
    explorer = DaznExplorer()
    try:
        await _show_tiles(explorer, section_id, section_title)
    finally:
        await explorer.close()

async def quick_search():
    explorer = DaznExplorer()
    try:
        await _search_menu(explorer)
    finally:
        await explorer.close()



