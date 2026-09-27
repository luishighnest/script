"""Estrazione headless: riusa il browser attivo, chiama Playback API, estrae chiavi DRM."""

import sys, json, re, asyncio, subprocess, base64, os, uuid as _uuid, time

from pathlib import Path

from playwright.async_api import async_playwright
from rich.console import Console

console = Console(safe_box=True, highlight=False)



WIDEVINE_SID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")

CDP_PORT = 9222



# Preferisce il .wvd incluso nel progetto, poi cerca sul desktop e path noti

_WVD_LOCAL = Path(__file__).resolve().parent.parent.parent / "device.wvd"

WVD_PATH = str(_WVD_LOCAL) if _WVD_LOCAL.exists() else None

for scan_path in ([] if WVD_PATH else [r"./", r"C:\Users\alecl\Desktop\l3-keys-main\l3-keys-main", r"C:\Users\alecl\Desktop\2225908683", r"C:\Users\alecl\Desktop"]):

    for root, dirs, files in os.walk(scan_path):

        for f in files:

            if f.lower().endswith(".wvd"):

                WVD_PATH = os.path.join(root, f)

                break

        if WVD_PATH: break

    if WVD_PATH: break



DEVICE_ID_FILE = Path(__file__).resolve().parent.parent.parent / "saved_profiles" / "profile_mpd" / "chrome_profile" / "device_id.txt"





# Sessione HTTP globale persistente con connection pooling
_GLOBAL_SESSION = None

def _load_proxy_worker() -> str:
    """Carica l'URL del Cloudflare Worker senza esporlo nel sorgente.

    Ordine: variabile d'ambiente DAZN_PROXY_WORKER, poi file locale gitignored
    (worker_url.txt / .worker_url). Nel repo resta solo un placeholder mascherato.
    """
    v = (os.environ.get("DAZN_PROXY_WORKER") or "").strip().rstrip("/")
    if v:
        return v
    for _name in ("worker_url.txt", ".worker_url"):
        _f = Path(__file__).resolve().parent.parent.parent / _name
        try:
            if _f.exists():
                _t = _f.read_text(encoding="utf-8").strip().rstrip("/")
                if _t:
                    return _t
        except Exception:
            pass
    return ""

PROXY_WORKER = _load_proxy_worker()

_CACHED_SERVICES = {
    "Playback": f"{PROXY_WORKER}/v5/Playback" if PROXY_WORKER else "https://api.playback.indazn.com/v5/Playback",
    "Rails": "https://rails.discovery.indazn.com/eu/v9/rails",
    "Rail": "https://rail.discovery.indazn.com/eu/v1/Rail",
    "Search": "https://search.discovery.indazn.com/v1/search",
    "Epg": "https://epg.discovery.indazn.com/eu/v1/epg",
    "ContentItem": "https://contentitem.discovery.indazn.com/eu/v1/contentitem",
    "Event": "https://event.discovery.indazn.com/eu/v1/event"
}
_CACHED_CDM = None

async def _get_http_session():
    global _GLOBAL_SESSION
    try:
        cur_loop = asyncio.get_running_loop()
    except RuntimeError:
        cur_loop = None
    sess_loop = getattr(_GLOBAL_SESSION, "_loop", None)
    if _GLOBAL_SESSION is None or (cur_loop is not None and sess_loop is not None and sess_loop != cur_loop):
        from curl_cffi.requests import AsyncSession
        _GLOBAL_SESSION = AsyncSession(impersonate="chrome142")
    return _GLOBAL_SESSION

class HeadlessExtractor:

    def __init__(self):
        self.result = {}

    def _device_id(self):
        profile_dir = getattr(self, '_profile_dir', None)
        did_file = Path(profile_dir) / "device_id.txt" if profile_dir else DEVICE_ID_FILE
        if did_file.exists():
            return did_file.read_text().strip()
        did = _uuid.uuid4().hex
        did_file.parent.mkdir(parents=True, exist_ok=True)
        did_file.write_text(did)
        return did

    def _decode_jwt_payload(self, tok):
        """Decodifica il payload di un JWT DAZN in modo robusto (base64url con padding)."""
        import base64 as _b64
        if not tok or not tok.startswith("eyJ"):
            return None
        try:
            parts = tok.split(".")
            if len(parts) < 2:
                return None
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            raw = _b64.urlsafe_b64decode(pad.encode("ascii"))
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            try:
                parts = tok.split(".")
                pad = parts[1] + "=" * (-len(parts[1]) % 4)
                raw = _b64.b64decode(pad.encode("ascii"))
                return json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                return None

    def _read_jwt_from_disk(self, profile_dir: Path) -> str:
        """Ritorna un JWT valido per l'Italia dal profilo (auth_token.json, poi leveldb).

        Accetta SOLO token con country == 'it' e NON scaduti: un token US o scaduto
        causerebbe l'errore Playback 10000. Return vuoto se non esiste un buon token.
        """
        import re, time
        if not profile_dir or not Path(profile_dir).exists():
            return ""
        p = Path(profile_dir)
        now = time.time()

        def _it_valid(tok):
            pl = self._decode_jwt_payload(tok)
            if not pl:
                return None
            country = str(pl.get("country") or "").lower()
            content_country = str(pl.get("contentCountry") or "").lower()
            if country and country not in ("it", "none", ""):
                return None
            if content_country and content_country not in ("it", "none", ""):
                return None
            # Tenta di scartare i token legacy privi di contentCountry se sono token base
            if not country and not content_country and "contentCountry" not in pl and "user" in pl:
                return None
            if pl.get("exp", 0) <= now:
                return None
            return pl

        # 1. Controlla prima i file dedicati dazn_session.json / auth_token.json
        possible_auth_files = [
            p / "dazn_session.json",
            p / "auth_token.json",
            p / "chrome_profile" / "dazn_session.json",
            p / "chrome_profile" / "auth_token.json",
            p.parent / "dazn_session.json",
            p.parent / "auth_token.json",
            p.parent / "chrome_profile" / "dazn_session.json",
            p.parent / "chrome_profile" / "auth_token.json",
        ]
        for auth_file in possible_auth_files:
            if auth_file.exists():
                try:
                    data = json.loads(auth_file.read_text(encoding="utf-8"))
                    tok = data.get("jwt")
                    did = data.get("device_id")
                    if did:
                        self._real_device_id = str(did).split("|")[0].strip()
                    if tok and tok.startswith("eyJ") and _it_valid(tok):
                        return tok
                except Exception:
                    pass

        # 2. Fallback: LevelDB del browser (solo token country == 'it')
        leveldb_dirs = [
            p / "Default" / "Local Storage" / "leveldb",
            p / "chrome_profile" / "Default" / "Local Storage" / "leveldb",
            p / "Local Storage" / "leveldb",
            p / "leveldb"
        ]
        candidates = []
        for ldir in leveldb_dirs:
            if ldir.exists():
                for f in sorted(list(ldir.glob("*.ldb")) + list(ldir.glob("*.log")), key=lambda x: x.stat().st_mtime, reverse=True):
                    try:
                        data = f.read_bytes()
                        if b"MISL.authToken" in data:
                            tokens = re.findall(rb'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', data)
                            for tok_b in tokens:
                                tok = tok_b.decode('ascii', errors='ignore')
                                pl = _it_valid(tok)
                                if pl:
                                    candidates.append((pl.get('exp', 0), tok))
                    except Exception:
                        pass
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

        # 3. Fallback: non pescare da altri profili se il profilo corrente ha un token specifico
        return ""

    async def _get_page_and_jwt(self, profile_dir=None):
        """Recupera il page object e JWT dal BrowserManager o direttamente dal profilo."""
        from dazn_navigator2.services.browser import get_browser, set_active_profile_dir
        target_p = Path(profile_dir) if profile_dir else None
        self._profile_dir = target_p
        if target_p:
            set_active_profile_dir(target_p)

        b = await get_browser(user_data_dir=target_p)
        jwt = ""

        # 1) Prima tenta di leggere il token di sessione live direttamente dal browser (localStorage)
        if b:
            await b.ensure_session()
            jwt_browser = await b.evaluate("localStorage.getItem('MISL.authToken')")
            pl_b = self._decode_jwt_payload(jwt_browser) if jwt_browser and jwt_browser.startswith("eyJ") else None
            if pl_b and pl_b.get("exp", 0) > time.time():
                jwt = jwt_browser
                console.print("[dim]  -> Token DAZN rinfrescato tramite browser[/dim]")

        # 2) Fallback su auth_token.json / leveldb se il browser non ha restituito un token valido
        if not jwt:
            jwt_disk = self._read_jwt_from_disk(target_p)
            if jwt_disk and jwt_disk.startswith("eyJ"):
                pl = self._decode_jwt_payload(jwt_disk)
                if pl:
                    jwt = jwt_disk
                    did_jwt = pl.get("deviceId", "").split("|")[0].strip()
                    if did_jwt:
                        self._real_device_id = did_jwt
                    c_val = pl.get('country') or pl.get('contentCountry') or 'it'
                    console.print(
                        f"[dim]  -> Token DAZN valido dal profilo (country={c_val}, "
                        f"scade tra {int(pl.get('exp', 0) - time.time())}s)[/dim]"
                    )

        if jwt:
            try:
                client = await _get_http_session()
                res_ref = await client.post(
                    "https://ott-authz-bff-prod.ar.indazn.com/v5/RefreshAccessToken",
                    headers={"authorization": f"Bearer {jwt}", "content-type": "application/json"},
                    timeout=10
                )
                if res_ref.status_code == 200:
                    ref_json = res_ref.json()
                    tok_fresh = ref_json.get("AuthToken", {}).get("Token")
                    if tok_fresh and tok_fresh.startswith("eyJ"):
                        jwt = tok_fresh
                        console.print("[dim]  -> Token DAZN rinfrescato a 24 ORE via RefreshAccessToken[/dim]")
                        if target_p:
                            for fname in ["dazn_session.json", "auth_token.json"]:
                                s_file = target_p / fname
                                if s_file.exists():
                                    try:
                                        data_s = json.loads(s_file.read_text(encoding="utf-8"))
                                        data_s["jwt"] = tok_fresh
                                        if "created_at" in data_s:
                                            data_s["created_at"] = int(time.time())
                                        s_file.write_text(json.dumps(data_s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                                    except Exception:
                                        pass
                            try:
                                from app import sync_to_github
                                sync_to_github(f"auto-refresh: aggiornato token JWT per {target_p.name}")
                            except Exception:
                                pass
            except Exception as e_ref:
                console.print(f"[dim]  -> Avviso RefreshAccessToken: {e_ref}[/dim]")

        if not jwt or not jwt.startswith("eyJ"):
            raise RuntimeError(
                "JWT Italia valido non trovato nel profilo DAZN. Il token salvato è scaduto o "
                "appartiene ad un account non italiano. Riesegui l'estrazione con una sessione "
                "italiana attiva oppure carica il profilo corretto."
            )

        pl = self._decode_jwt_payload(jwt)
        if pl:
            did_jwt = pl.get("deviceId", "").split("|")[0].strip()
            if did_jwt:
                self._real_device_id = did_jwt
            console.print(
                f"[dim]  -> JWT usato per Playback: country={pl.get('country')}, "
                f"exp in {int(pl.get('exp', 0) - time.time())}s[/dim]"
            )

        if not getattr(self, "_real_device_id", None) and b:
            try:
                stored_did = await b.evaluate("localStorage.getItem('MISL.deviceId') || localStorage.getItem('dazn.deviceId')")
                if stored_did:
                    self._real_device_id = stored_did
            except Exception:
                pass

        if b:
            # Sincronizza i cookie del browser (CloudFront, sessione DAZN) nella sessione curl_cffi
            try:
                cookies = await b.context.cookies()
                client = await _get_http_session()
                for c in cookies:
                    client.cookies.set(c["name"], c["value"], domain=c.get("domain", ".dazn.com"))
            except Exception:
                pass

        # Persist JWT to auth_token.json and dazn_session.json for future runs
        if jwt and jwt.startswith("eyJ") and target_p:
            try:
                auth_file = Path(target_p) / "auth_token.json"
                auth_file.write_text(json.dumps({"jwt": jwt}), encoding="utf-8")
                
                # Aggiorna anche dazn_session.json se presente
                session_file = Path(target_p) / "dazn_session.json"
                if not session_file.exists():
                    session_file = Path(target_p).parent / "dazn_session.json"
                if session_file.exists():
                    try:
                        sdata = json.loads(session_file.read_text(encoding="utf-8"))
                        sdata["jwt"] = jwt
                        sdata["created_at"] = int(time.time())
                        session_file.write_text(json.dumps(sdata, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            except Exception:
                pass

        return (b.page if b else None), jwt


    async def _chiama_api(self, url, jwt, method="GET", body_obj=None, page=None, cdn_token=None):
        import json
        from dazn_navigator2.settings import get_setting
        engine = get_setting("extraction_engine")
        
        dev_id = getattr(self, "_real_device_id", None) or self._device_id()

        headers = {
            "authorization": f"Bearer {jwt}",
            "dazn-token": cdn_token if cdn_token else jwt,
            "x-dazn-device": dev_id,
            "content-type": "application/json",
            "accept": "*/*",
        }
        
        if engine == "headless" and page:
            try:
                clean_hdrs = dict(headers)
                clean_hdrs.pop("origin", None)
                clean_hdrs.pop("referer", None)
                if method == "POST":
                    resp = await page.request.post(url, headers=clean_hdrs, data=json.dumps(body_obj) if body_obj else None)
                else:
                    resp = await page.request.get(url, headers=clean_hdrs)
                body_txt = await resp.text()
                if resp.status < 500:
                    return {"ok": resp.ok, "status": resp.status, "body": body_txt, "fallback": True}
            except Exception:
                pass

        # Modalità Veloce (curl_cffi con Sessione persistente)
        try:
            from curl_cffi.requests import AsyncSession
            client = await _get_http_session()
            if method == "POST":
                resp = await client.post(url, headers=headers, json=body_obj, timeout=10)
            else:
                resp = await client.get(url, headers=headers, timeout=10)
            
            if resp.status_code < 400:
                return {"ok": True, "status": resp.status_code, "body": resp.text, "type": resp.headers.get("content-type", ""), "fallback": False}
            
            # Se la risposta HTTP dà errore, fallback su page.evaluate nativo dal browser
            if page:
                try:
                    js_code = """
                    async ({url, method, headers, body}) => {
                        try {
                            const clean_headers = {...headers};
                            delete clean_headers['origin'];
                            delete clean_headers['referer'];
                            delete clean_headers['user-agent'];
                            const opts = { method: method, headers: clean_headers };
                            if (body !== null) opts.body = JSON.stringify(body);
                            const resp = await fetch(url, opts);
                            const text = await resp.text();
                            return {ok: resp.ok, status: resp.status, body: text, type: resp.headers.get("content-type") || "", fallback: true};
                        } catch(e) {
                            return {ok: false, error: e.name + ': ' + e.message};
                        }
                    }
                    """
                    res = await page.evaluate(js_code, {"url": url, "method": method, "headers": headers, "body": body_obj})
                    if res and res.get("ok"):
                        return res
                except Exception:
                    pass
            return {"ok": False, "status": resp.status_code, "body": resp.text}
        except Exception as e:
            if page:
                try:
                    js_code = """
                    async ({url, method, headers, body}) => {
                        try {
                            const clean_headers = {...headers};
                            delete clean_headers['origin'];
                            delete clean_headers['referer'];
                            delete clean_headers['user-agent'];
                            const opts = { method: method, headers: clean_headers, credentials: 'include' };
                            if (body !== null) opts.body = JSON.stringify(body);
                            const resp = await fetch(url, opts);
                            const text = await resp.text();
                            return {ok: resp.ok, status: resp.status, body: text, type: resp.headers.get("content-type") || "", fallback: true};
                        } catch(e) {
                            return {ok: false, error: e.name + ': ' + e.message};
                        }
                    }
                    """
                    res = await page.evaluate(js_code, {"url": url, "method": method, "headers": headers, "body": body_obj})
                    if res and res.get("ok"):
                        return res
                except Exception as ex:
                    err_str = str(ex).lower()
                    if "execution context was destroyed" in err_str or "navigation" in err_str:
                        await asyncio.sleep(0.5)
                        try:
                            res = await page.evaluate(js_code, {"url": url, "method": method, "headers": headers, "body": body_obj})
                            if res and res.get("ok"):
                                return res
                        except Exception:
                            pass
            return {"ok": False, "error": str(e)}

    async def estrai(self, profile_dir, asset_id, titolo="") -> dict:
        """Estrae MPD, PSSH, licenza e chiavi in modo istantaneo."""
        global _CACHED_SERVICES, _CACHED_CDM
        self.result = {"ok": False, "mpd_url": None, "pssh": None, "keys": None, "ext_url": None, "error": None}

        if not WVD_PATH:
            self.result["error"] = "File .wvd non trovato."
            return self.result

        import time
        _t = time.time()
        page, jwt = await self._get_page_and_jwt(profile_dir)
        console.print(f"[dim]  -> 1. Get JWT: {time.time() - _t:.2f}s[/dim]")

        pl_jwt = self._decode_jwt_payload(jwt) or {}
        jwt_did = pl_jwt.get("deviceId")
        dev_id = jwt_did if jwt_did else (getattr(self, "_real_device_id", None) or self._device_id())
        if dev_id:
            dev_id = dev_id.split("|")[0].strip()

        from dazn_navigator2.settings import get_setting
        mfr = (get_setting("playback_manufacturer") or "web").lower()
        if mfr in ("samsung", "android"):
            platform_param = "android"
            player_id_param = "%40dazn%2Fpeng-android%2Fandroid"
            model_param = "SM-A137F"
            mfr_param = "samsung"
        else:
            platform_param = "web"
            player_id_param = "%40dazn%2Fpeng-html5-core%2Fweb%2Fweb"
            model_param = "unknown"
            mfr_param = "Web"

        playback_svc = "https://api.playback.indazn.com/v5/Playback"
        console.print(f"[dim]  -> Playback endpoint: {playback_svc}[/dim]")
        _t = time.time()
        sid = f"{int(time.time()*1000)}-{dev_id}-{asset_id}-{_uuid.uuid4().hex[:8].upper()}"
        pb_url = (f"{playback_svc}?AppVersion=2.85.0&DrmType=WIDEVINE&Format=MPEG-DASH"
                  f"&PlayerId=%40dazn%2Fpeng-html5-core%2Fweb%2Fweb&Platform=web&Model=Desktop"
                  f"&Secure=true&Manufacturer=Web&PlayReadyInitiator=false&Capabilities=hcst%2Cmta"
                  f"&AssetId={asset_id}&LanguageCode=it&country=it&CountryCode=it")

        web_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

        pb_r = await self._chiama_api(pb_url, jwt, page=page)

        console.print(f"[dim]  -> 3. Playback API: {time.time() - _t:.2f}s[/dim]")

        if not pb_r.get("ok"):
            # Fallback intelligente per canali lineari / eventi: se l'asset_id EPG fallisce o è terminato, cerca l'asset_id attivo da Rail o Search
            found_fallback = False
            try:
                # 1. Tenta prima la ricerca sui canali Live attivi in Rail API
                if page:
                    r_rail = await page.request.get(
                        "https://rail.discovery.indazn.com/eu/v1/Rail?id=live&country=it&language=it",
                        headers={"authorization": f"Bearer {jwt}", "x-dazn-device": dev_id, "user-agent": web_ua}
                    )
                    if r_rail.ok:
                        r_data = await r_rail.json()
                        live_tiles = r_data.get("Tiles", [])
                        match = None
                        if titolo:
                            for t in live_tiles:
                                if titolo.strip().lower() in t.get("Title", "").strip().lower():
                                    match = t
                                    break
                        
                        if match and match.get("AssetId") != asset_id:
                            fb_aid = match.get("AssetId")
                            console.print(f"[dim]  -> Fallback su evento Live attivo: {match.get('Title')} ({fb_aid})[/dim]")
                            fb_url = f"{playback_svc}?AppVersion=0.149.9&DrmType=WIDEVINE&Format=MPEG-DASH&PlayerId=%40dazn%2Fpeng-html5-core%2Fweb%2Fweb&Platform=web&Model=unknown&Secure=true&Manufacturer=Web&PlayReadyInitiator=false&Capabilities=hcst%2Cmta&AssetId={fb_aid}&LanguageCode=it&country=it&CountryCode=it"
                            r_fb_req = await page.request.get(fb_url, headers={
                                "authorization": f"Bearer {jwt}",
                                "dazn-token": jwt,
                                "x-dazn-device": dev_id,
                                "user-agent": web_ua
                            })
                            if r_fb_req.ok:
                                pb_r = {"ok": True, "status": r_fb_req.status, "body": await r_fb_req.text()}
                                found_fallback = True
            except Exception:
                pass

            if not found_fallback:
                err_detail = pb_r.get("error") or pb_r.get("body") or f"HTTP {pb_r.get('status', 'sconosciuto')}"
                try:
                    err_json = json.loads(pb_r.get("body", "{}"))
                    odata = err_json.get("odata.error", {})
                    code = odata.get("code")
                    msg = odata.get("message", {}).get("value", "")
                    if code == 10803 or "Eligibility" in msg:
                        err_detail = "Contenuto non incluso nel tuo abbonamento o evento terminato (Eligibility not allowed)."
                    elif msg:
                        err_detail = f"{msg} (Codice: {code})"
                except Exception:
                    pass
                self.result["error"] = f"Playback API: {err_detail}"
                return self.result

        pb = json.loads(pb_r["body"])
        pbd = pb.get("PlaybackDetails") or []
        if not pbd:
            self.result["error"] = "Nessun PlaybackDetails nella risposta."
            return self.result

        det = pbd[0]
        mpd_url_original = det.get("ManifestUrl", "")
        la_url = det.get("LaUrl", "")
        cdn_tok = det.get("CdnToken", {})
        cdn_name = cdn_tok.get("Name", "") if cdn_tok else ""
        cdn_value = cdn_tok.get("Value", "") if cdn_tok else ""

        if not mpd_url_original or not la_url:
            self.result["error"] = "MPD o License URL mancanti."
            return self.result

        self.result["mpd_url"] = mpd_url_original
        
        from dazn_navigator2.settings import get_setting
        engine = get_setting("extraction_engine")

        if engine == "headless" and page:
            ua = await page.evaluate("navigator.userAgent")
        else:
            # Recupera l'esatto User-Agent usato da curl_cffi per garantire che l'hash 'ua' nel dazn-token coincida
            client = await _get_http_session()
            ua = getattr(client, "_user_agent", None) or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        
        dazn_token = cdn_value if cdn_value else (jwt if jwt else "")

        # Itera i PlaybackDetails per trovare la CDN funzionante (evita 401 Forbidden-682 su Akamai)
        client = await _get_http_session()
        import urllib.parse
        mpd_r = {"ok": False, "error": "Nessuna CDN valida"}
        chosen_pbd = None
        chosen_mpd_url = ""
        chosen_la_url = ""
        chosen_token = ""
        chosen_cdn_name = ""
        chosen_fetch_url = ""

        _t = time.time()
        pbd_sorted = sorted(pbd, key=lambda x: 0 if "indazn.com" in x.get("ManifestUrl", "").lower() else 1)
        for cand_pbd in pbd_sorted:
            c_mpd = cand_pbd.get("ManifestUrl", "")
            c_la = cand_pbd.get("LaUrl", "")
            c_tok_obj = cand_pbd.get("CdnToken", {}) or {}
            c_name = c_tok_obj.get("Name", "") or "dazn-token"
            c_val = c_tok_obj.get("Value", "")

            if not c_mpd or not c_la:
                continue

            c_tok = c_val if c_val else jwt
            c_fetch_url = c_mpd
            if c_val:
                if c_val.startswith("eyJ") and "daznedge.net" not in c_mpd:
                    if "://" in c_mpd:
                        proto, rest = c_mpd.split("://", 1)
                        if "/" in rest:
                            host, path = rest.split("/", 1)
                            c_fetch_url = f"{proto}://{host}/@{c_val}/{path}"
                        else:
                            c_fetch_url = f"{proto}://{rest}/@{c_val}"
                else:
                    sep = "&" if "?" in c_fetch_url else "?"
                    c_fetch_url = f"{c_fetch_url}{sep}{c_name}={c_val}"

            c_hdrs = {
                "origin": "https://www.dazn.com",
                "referer": "https://www.dazn.com/",
                "dazn-token": c_tok,
                "accept": "*/*"
            }

            try:
                r_resp = await client.get(c_fetch_url, headers=c_hdrs, timeout=6)
                if r_resp.status_code == 200:
                    mpd_r = {"ok": True, "status": 200, "body": r_resp.text}
                elif page:
                    mpd_r = await page.evaluate(
                        """async ({url, token}) => {
                            try {
                                const r = await fetch(url, { headers: { "dazn-token": token } });
                                return { ok: r.ok, status: r.status, body: await r.text() };
                            } catch(e) { return { ok: false, error: e.message }; }
                        }""",
                        {"url": c_fetch_url, "token": c_tok}
                    )
                elif PROXY_WORKER:
                    worker_mpd = f"{PROXY_WORKER}/?target={urllib.parse.quote(c_fetch_url)}"
                    r_worker = await client.get(worker_mpd, headers=c_hdrs, timeout=6)
                    if r_worker.status_code == 200:
                        mpd_r = {"ok": True, "status": 200, "body": r_worker.text}
                    else:
                        mpd_r = {"ok": False, "status": r_worker.status_code, "body": r_worker.text}
                else:
                    mpd_r = {"ok": False, "status": r_resp.status_code, "body": r_resp.text}
            except Exception as e:
                if PROXY_WORKER:
                    try:
                        worker_mpd = f"{PROXY_WORKER}/?target={urllib.parse.quote(c_fetch_url)}"
                        r_worker = await client.get(worker_mpd, headers=c_hdrs, timeout=6)
                        if r_worker.status_code == 200:
                            mpd_r = {"ok": True, "status": 200, "body": r_worker.text}
                        else:
                            mpd_r = {"ok": False, "status": r_worker.status_code, "body": r_worker.text}
                    except Exception as we:
                        mpd_r = {"ok": False, "error": str(we)}
                else:
                    mpd_r = {"ok": False, "error": str(e)}

            if mpd_r.get("ok"):
                chosen_pbd = cand_pbd
                chosen_mpd_url = c_mpd
                chosen_la_url = c_la
                chosen_token = c_tok
                chosen_cdn_name = c_name
                chosen_fetch_url = c_fetch_url
                break

        console.print(f"[dim]  -> 4. Fetch MPD: {time.time() - _t:.2f}s[/dim]")

        if not mpd_r.get("ok") or not chosen_pbd:
            err_detail = mpd_r.get('body', '') or mpd_r.get('error', '')
            self.result["error"] = f"Fetch MPD: {mpd_r.get('status','?')} - Dettaglio server: {err_detail}"
            console.print(f"[bold red]  [Dettaglio Errore MPD][/bold red] Status: {mpd_r.get('status')} | Risposta Server: {err_detail}")
            return self.result

        mpd_url_original = chosen_mpd_url
        la_url = chosen_la_url
        dazn_token = chosen_token if chosen_token else jwt
        cdn_name = chosen_cdn_name
        fetch_mpd_url = chosen_fetch_url
        self.result["mpd_url"] = mpd_url_original

        _t = time.time()
        import xml.etree.ElementTree as ET
        for el in ET.fromstring(mpd_r["body"]).iter():
            if el.tag.endswith("}pssh"):
                b64 = (el.text or "").strip()
                if b64 and base64.b64decode(b64)[12:28] == WIDEVINE_SID:
                    self.result["pssh"] = b64
                    break

        if not self.result["pssh"]:
            self.result["error"] = "PSSH Widevine non trovato nel MPD."
            return self.result

        from pywidevine import PSSH, Cdm, Device
        if _CACHED_CDM is None:
            _CACHED_CDM = Cdm.from_device(Device.load(WVD_PATH))

        cdm = _CACHED_CDM
        sess = cdm.open()
        chal = cdm.get_license_challenge(sess, PSSH(base64.b64decode(self.result["pssh"])))

        pl_jwt = self._decode_jwt_payload(jwt) or {}
        jwt_did = pl_jwt.get("deviceId") or ""
        
        # Prepara varianti di ID per x-daznid / x-dazn-device
        did_variants = []
        if jwt_did:
            did_variants.append(jwt_did)
            did_clean = jwt_did.split("|")[0].strip()
            if did_clean not in did_variants:
                did_variants.append(did_clean)
            if "-" in did_clean:
                uuid_only = "-".join(did_clean.split("-")[:5])
                if uuid_only not in did_variants:
                    did_variants.append(uuid_only)
        
        real_id = getattr(self, "_real_device_id", None) or self._device_id()
        if real_id and real_id not in did_variants:
            did_variants.append(real_id)

        lr = None
        client = await _get_http_session()

        for cand_id in did_variants:
            lic_hdrs = {
                "content-type": "application/octet-stream",
                "user-agent": ua,
                "authorization": f"Bearer {jwt}",
                "dazn-token": dazn_token,
                "x-dazn-token": dazn_token,
                "x-brand": "DAZN",
                "x-daznid": cand_id,
                "x-dazn-device": cand_id,
                "x-correlation-id": str(_uuid.uuid4()),
            }
            lic_hdrs_clean = {
                "content-type": "application/octet-stream",
                "user-agent": ua,
                "authorization": f"Bearer {jwt}",
                "dazn-token": dazn_token,
                "x-dazn-token": dazn_token,
                "x-brand": "DAZN",
                "x-daznid": cand_id,
                "x-dazn-device": cand_id,
                "x-correlation-id": str(_uuid.uuid4()),
            }

            if page:
                try:
                    lic_hdrs["user-agent"] = "Mozilla/5.0 (Linux; Android 12; SM-A137F Build/SP1A.210812.016; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/131.0.6778.135 Mobile Safari/537.36"
                    lic_resp = await page.request.post(
                        la_url,
                        headers=lic_hdrs,
                        data=chal
                    )
                    if lic_resp.ok:
                        lr = {"ok": True, "body": base64.b64encode(await lic_resp.body()).decode("ascii")}
                        break
                    else:
                        lr = {"ok": False, "status": lic_resp.status, "bodyText": await lic_resp.text()}
                except Exception as ex:
                    lr = {"ok": False, "error": f"Browser request exception: {ex}"}
            else:
                try:
                    lic_resp = await client.post(la_url, headers=lic_hdrs, data=chal, timeout=10)
                    if lic_resp.status_code == 200:
                        lr = {"ok": True, "body": base64.b64encode(lic_resp.content).decode("ascii")}
                        break
                    else:
                        if PROXY_WORKER:
                            import urllib.parse
                            worker_la = f"{PROXY_WORKER}/?target={urllib.parse.quote(la_url)}"
                            r_w = await client.post(worker_la, headers=lic_hdrs, data=chal, timeout=10)
                            if r_w.status_code == 200:
                                lr = {"ok": True, "body": base64.b64encode(r_w.content).decode("ascii")}
                                break
                            else:
                                lr = {
                                    "ok": False,
                                    "status": r_w.status_code,
                                    "bodyText": r_w.text,
                                    "headers": dict(r_w.headers),
                                    "browser_res": lr
                                }
                        else:
                            lr = {
                                "ok": False,
                                "status": lic_resp.status_code,
                                "bodyText": lic_resp.text,
                                "headers": dict(lic_resp.headers),
                                "browser_res": lr
                            }
                except Exception as e:
                    if PROXY_WORKER:
                        try:
                            import urllib.parse
                            worker_la = f"{PROXY_WORKER}/?target={urllib.parse.quote(la_url)}"
                            r_w = await client.post(worker_la, headers=lic_hdrs, data=chal, timeout=10)
                            if r_w.status_code == 200:
                                lr = {"ok": True, "body": base64.b64encode(r_w.content).decode("ascii")}
                                break
                            else:
                                lr = {"ok": False, "status": r_w.status_code, "bodyText": r_w.text}
                        except Exception as we:
                            lr = {"ok": False, "error": str(we)}
                    else:
                        if not lr:
                            lr = {"ok": False, "error": str(e)}

        if not lr or not lr.get("ok"):
            err_msg = f"Licenza: {lr.get('status','?')} - Motivo: {lr.get('statusText', '')} {lr.get('bodyText', '')[:300]} {lr.get('error', '')}".strip()
            self.result["error"] = err_msg
            console.print(f"[bold red]  [Dettaglio Errore Licenza DRM][/bold red]")
            console.print(f"    * Status HTTP: [bold yellow]{lr.get('status')}[/bold yellow]")
            if lr.get("statusText"):
                console.print(f"    * Status Text: {lr.get('statusText')}")
            if lr.get("bodyText"):
                console.print(f"    * Corpo Risposta Server: [dim]{lr.get('bodyText')[:400]}[/dim]")
            if lr.get("error"):
                console.print(f"    * Errore Exception: [red]{lr.get('error')}[/red]")
            if lr.get("headers"):
                server_hdr = lr.get("headers", {}).get("server") or lr.get("headers", {}).get("Server")
                cf_id = lr.get("headers", {}).get("x-amz-cf-id")
                console.print(f"    * Server: {server_hdr} (CF-ID: {cf_id})")
            cdm.close(sess)
            return self.result

        cdm.parse_license(sess, base64.b64decode(lr["body"]))
        keys = [f"{k.kid.hex}:{k.key.hex()}" for k in cdm.get_keys(sess) if k.type == "CONTENT"]
        cdm.close(sess)
        console.print(f"[dim]  -> 6. Licenza DRM: {time.time() - _t:.2f}s[/dim]")

        if not keys:

            self.result["error"] = "Nessuna chiave CONTENT."

            return self.result



        import urllib.parse

        self.result["keys"] = keys

        kid_hex, key_hex = keys[0].split(":")

        ck = base64.b64encode(json.dumps({kid_hex: key_hex}).encode("utf-8")).decode("utf-8")

        hdrs_b64 = base64.b64encode(json.dumps({
            "user-agent": ua,
            "referer": "https://www.dazn.com/",
            "origin": "https://www.dazn.com",
            "dazn-token": dazn_token,
        }).encode("utf-8")).decode("utf-8")

        ext_mpd = chosen_fetch_url if chosen_fetch_url else mpd_url_original

        self.result["ext_url"] = (
            "extension://opmeopcambhfimffbomjgemehjkbbmji/pages/player.html#"
            f"{ext_mpd}&ck={urllib.parse.quote(ck)}&headers={urllib.parse.quote(hdrs_b64)}"
        )

        self.result["mpd"] = ext_mpd
        self.result["kodi_url"] = f"{fetch_mpd_url}&ck={ck}&headers={hdrs_b64}"

        self.result["dazn_token"] = dazn_token
        self.result["jwt"] = jwt
        self.result["cdn_name"] = cdn_name

        self.result["ok"] = True

        self.result["titolo"] = titolo



        return self.result







