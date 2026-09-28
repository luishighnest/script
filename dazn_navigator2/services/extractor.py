"""Estrazione headless: riusa il browser attivo, chiama Playback API, estrae chiavi DRM."""

import sys, json, re, asyncio, subprocess, base64, os, uuid as _uuid, time, threading

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

# ─── Velocita': quanto token resta considerato "buono" ────────────────────
# Sotto TOKEN_MIN_LIFE_S il token e' scaduto o in scadenza: si passa dal
# profilo su disco al browser (l'unica fonte che puo' rinnovarlo davvero).
TOKEN_MIN_LIFE_S = 300
# Sopra TOKEN_REFRESH_THRESHOLD non si chiama RefreshAccessToken: il token
# dura ore, rifarlo a ogni estrazione costa una chiamata di rete, la
# riscrittura dei file del profilo e un commit/push su GitHub.
TOKEN_REFRESH_THRESHOLD = 3600


def _push_token_in_background(target_p):
    """Committa il token aggiornato senza bloccare la richiesta HTTP.

    sync_to_github() esegue git add/commit/push: in mezzo alla richiesta di
    estrazione aggiunge secondi di attesa all'utente per un file che puo'
    aspettare. Se il thread fallisce pazienza: il token e' gia' su disco.
    """
    def _worker():
        try:
            from app import sync_to_github
            sync_to_github(f"auto-refresh: aggiornato token JWT per {target_p.name}")
        except Exception:
            pass
    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        pass

# ─── User-Agent: unica fonte di verità per MPD e segmenti ───────────────
# Il token CDN di DAZN (JWT nel path /@token/ o header dazn-token) contiene
# il claim "headers":["user-agent"]: la CDN rifiuta (HTTP 401) qualsiasi
# richiesta il cui User-Agent non coincide con quello usato per ottenere il
# token dalla Playback API. Per questo l'UA va SEMPRE rilevato dalla sessione
# HTTP reale e propagato a estrattore, playlist e addon.
DEFAULT_IMPERSONATE = "chrome142"

# Rilevamento UA: cache in memoria + cache su disco (config.json)
_UA_MEMORY_CACHE = {}
_UA_LOCK = threading.Lock()

UA_PROBE_URLS = (
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://httpbin.org/user-agent",
    "https://api.ipify.org/?format=json",
)

# Candidati di fallback (dal piu' probabile): vengono verificati contro la CDN
_FALLBACK_UA_CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36"
)

# Player Android: usato solo come fallback per la richiesta di licenza
ANDROID_PLAYER_UA = (
    "Mozilla/5.0 (Linux; Android 12; SM-A137F Build/SP1A.210812.016; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/131.0.6778.135 Mobile Safari/537.36"
)


def _valid_browser_ua(ua: str) -> bool:
    """True se la stringa e' un User-Agent di browser (non Kodi, non vuoto)."""
    if not ua or not isinstance(ua, str):
        return False
    u = ua.strip()
    if not u or len(u) < 20:
        return False
    low = u.lower()
    if "kodi" in low or "libcurl" in low or "python-requests" in low:
        return False
    return any(t in low for t in ("mozilla/5.0", "chrome/", "safari/", "firefox/", "edg/"))


def get_impersonate() -> str:
    """Profilo TLS curl_cffi da usare (config: preferred_impersonate)."""
    from dazn_navigator2.settings import get_setting
    imp = (get_setting("preferred_impersonate") or "").strip()
    if not imp:
        return DEFAULT_IMPERSONATE
    try:
        from curl_cffi.requests.impersonate import BrowserType
        allowed = {b.value for b in BrowserType}
    except Exception:
        allowed = set()
    if allowed and imp in allowed:
        return imp
    if allowed:
        return DEFAULT_IMPERSONATE if DEFAULT_IMPERSONATE in allowed else imp
    return imp


def _probe_user_agent_sync(impersonate: str = None) -> str:
    """Chiede a un echo server l'UA reale inviato dalla sessione curl_cffi."""
    imp = impersonate or get_impersonate()
    try:
        from curl_cffi import requests as creq
    except Exception:
        return ""
    for url in UA_PROBE_URLS:
        try:
            s = creq.Session(impersonate=imp)
            try:
                r = s.get(url, timeout=6)
                txt = r.text or ""
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            ua = ""
            for line in txt.splitlines():
                if line.startswith("uag="):
                    ua = line[4:].strip()
                    break
            if not ua and '"user-agent"' in txt:
                import json as _json
                try:
                    ua = _json.loads(txt).get("user-agent", "")
                except Exception:
                    ua = ""
            if _valid_browser_ua(ua):
                return ua.strip()
        except Exception:
            continue
    return ""


def detect_user_agent(impersonate: str = None) -> str:
    """UA reale della sessione: cache -> config.json -> probing -> fallback."""
    imp = impersonate or get_impersonate()
    with _UA_LOCK:
        cached = _UA_MEMORY_CACHE.get(imp)
        if cached:
            return cached
        from dazn_navigator2.settings import get_setting, save_config, load_config
        stored = (get_setting("curl_user_agent") or "").strip()
        if stored:
            # La config puo' essere obsoleta: si valida con un probing leggero
            # solo se non e' gia' stata verificata in questa sessione.
            probed = _probe_user_agent_sync(imp)
            ua = probed or (stored if _valid_browser_ua(stored) else "")
        else:
            ua = _probe_user_agent_sync(imp)
        if not ua:
            major = "".join(ch for ch in imp if ch.isdigit()) or "131"
            ua = _FALLBACK_UA_CHROME.format(v=major)
        _UA_MEMORY_CACHE[imp] = ua
        try:
            cfg = load_config()
            if cfg.get("curl_user_agent") != ua:
                cfg["curl_user_agent"] = ua
                cfg.setdefault("preferred_impersonate", imp)
                save_config(cfg)
        except Exception:
            pass
        return ua


def get_user_agent() -> str:
    """UA da usare per MPD/segmenti (sincrono, usabile anche dall'addon Kodi)."""
    return detect_user_agent()


async def get_session_user_agent(client=None) -> str:
    """Variante asincrona: riusa la sessione gia' aperta se disponibile."""
    with _UA_LOCK:
        cached = _UA_MEMORY_CACHE.get(get_impersonate())
    if cached:
        return cached
    if client is not None:
        for url in UA_PROBE_URLS:
            try:
                r = await client.get(url, timeout=6)
                txt = r.text or ""
                ua = ""
                for line in txt.splitlines():
                    if line.startswith("uag="):
                        ua = line[4:].strip()
                        break
                if not ua and "user-agent" in txt:
                    import json as _json
                    try:
                        ua = _json.loads(txt).get("user-agent", "")
                    except Exception:
                        ua = ""
                if _valid_browser_ua(ua):
                    with _UA_LOCK:
                        _UA_MEMORY_CACHE[get_impersonate()] = ua.strip()
                    return ua.strip()
            except Exception:
                continue
    return detect_user_agent()


def ua_candidates() -> list:
    """Lista ordinata di User-Agent da provare contro la CDN (auto-riparazione)."""
    from dazn_navigator2.settings import get_setting
    out = []
    for ua in (detect_user_agent(), (get_setting("curl_user_agent") or "").strip()):
        if _valid_browser_ua(ua) and ua not in out:
            out.append(ua)
    for v in ("131", "136", "142", "145", "146", "124", "123"):
        ua = _FALLBACK_UA_CHROME.format(v=v)
        if ua not in out:
            out.append(ua)
    return out


def _cdn_headers(ua: str, dazn_token: str = "") -> dict:
    h = {
        "origin": "https://www.dazn.com",
        "referer": "https://www.dazn.com/",
        "accept": "*/*",
    }
    if ua:
        h["user-agent"] = ua
    if dazn_token:
        h["dazn-token"] = dazn_token
    return h


def validate_cdn_user_agent(mpd_url: str, dazn_token: str = "", candidates=None) -> str:
    """Verifica quale User-Agent e' realmente accettato dalla CDN per questo MPD.

    Il token e' legato all'UA della richiesta Playback: si testano i candidati e
    si restituisce il primo che restituisce HTTP 200 (stringa vuota se nessuno).
    """
    if not mpd_url:
        return ""
    try:
        from curl_cffi import requests as creq
        cands = [c for c in (candidates if candidates is not None else ua_candidates()) if c]
        for ua in cands:
            try:
                s = creq.Session(impersonate=get_impersonate())
                try:
                    r = s.get(mpd_url, headers=_cdn_headers(ua, dazn_token), timeout=8)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
                if r.status_code == 200:
                    return ua
            except Exception:
                continue
    except Exception:
        pass
    return ""


_CACHED_SERVICES = {
    "Playback": "https://api.playback.indazn.com/v5/Playback",
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
        _GLOBAL_SESSION = AsyncSession(impersonate=get_impersonate())
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
            Path(r"C:\Users\alecl\Desktop\chrome_profile\dazn_session.json"),
            Path(r"C:\Users\alecl\Desktop\chrome_profile\auth_token.json"),
            Path(r"C:\Users\alecl\Desktop\dazn_session.json"),
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
            Path(r"C:\Users\alecl\Desktop\chrome_profile\Default\Local Storage\leveldb"),
            Path(r"C:\Users\alecl\Desktop\chrome_profile\Local Storage\leveldb"),
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

    async def _get_page_and_jwt(self, profile_dir=None, force_browser=False):
        """Recupera il page object e JWT dal profilo, o dal browser se serve.

        Percorso veloce: se il profilo su disco ha gia' un token valido non si
        avvia affatto il browser. Lanciarlo costa ~6s di context + ~17s di
        navigazione su dazn.com per ogni evaluate() (che richiama
        ensure_session()), e il suo localStorage restituisce spesso 'None'
        anche quando il token su disco e' perfettamente valido: 30s sprecati
        per arrivare allo stesso token che si legge in 0.02s.
        """
        from dazn_navigator2.services.browser import get_browser, set_active_profile_dir
        from dazn_navigator2.settings import get_setting
        target_p = Path(profile_dir) if profile_dir else None
        self._profile_dir = target_p
        self._browser_used = False
        if target_p:
            set_active_profile_dir(target_p)

        engine = get_setting("extraction_engine")
        need_page = force_browser or (engine == "headless")

        # ─── FAST PATH: token valido gia' su disco, nessun browser ───
        jwt = ""
        if target_p and not force_browser:
            jwt_disk = self._read_jwt_from_disk(target_p)
            if jwt_disk and jwt_disk.startswith("eyJ"):
                pl = self._decode_jwt_payload(jwt_disk)
                if pl and int(pl.get("exp", 0)) - time.time() > TOKEN_MIN_LIFE_S:
                    jwt = jwt_disk
                    did_jwt = (pl.get("deviceId") or "").split("|")[0].strip()
                    if did_jwt:
                        self._real_device_id = did_jwt
                    c_val = pl.get('country') or pl.get('contentCountry') or 'it'
                    console.print(
                        f"[dim]  -> Token dal profilo (country={c_val}, valido "
                        f"{int(pl.get('exp', 0) - time.time()) // 60} min) - browser saltato[/dim]"
                    )

        b = None
        if need_page or not jwt:
            self._browser_used = True
            b = await get_browser(user_data_dir=target_p)
            if b:
                await b.ensure_session()
            # 1) token di sessione live dal browser (localStorage)
            if b and not jwt:
                jwt_browser = await b.evaluate("localStorage.getItem('MISL.authToken')")
                pl_b = self._decode_jwt_payload(jwt_browser) if jwt_browser and jwt_browser.startswith("eyJ") else None
                if pl_b and pl_b.get("exp", 0) > time.time():
                    jwt = jwt_browser
                    console.print("[dim]  -> Token DAZN rinfrescato tramite browser[/dim]")
                elif not jwt:
                    # 2) fallback su auth_token.json / leveldb
                    jwt_disk = self._read_jwt_from_disk(target_p)
                    if jwt_disk and jwt_disk.startswith("eyJ"):
                        pl = self._decode_jwt_payload(jwt_disk)
                        if pl and int(pl.get("exp", 0)) - time.time() > TOKEN_MIN_LIFE_S:
                            jwt = jwt_disk
                            console.print("[dim]  -> Token DAZN valido dal profilo (browser)[/dim]")

        if not jwt or not jwt.startswith("eyJ"):
            raise RuntimeError(
                "JWT Italia valido non trovato nel profilo DAZN. Il token salvato è scaduto o "
                "appartiene ad un account non italiano. Riesegui l'estrazione con una sessione "
                "italiana attiva oppure carica il profilo corretto."
            )

        pl = self._decode_jwt_payload(jwt)
        if pl:
            did_jwt = (pl.get("deviceId") or "").split("|")[0].strip()
            if did_jwt:
                self._real_device_id = did_jwt
            console.print(
                f"[dim]  -> JWT usato per Playback: country={pl.get('country')}, "
                f"exp in {int(pl.get('exp', 0) - time.time())}s[/dim]"
            )

        # ─── Refresh solo se il token sta davvero per scadere ───
        # RefreshAccessToken porta a 24h: rifarlo a ogni estrazione e' uno
        # spreco di rete, scrittura su disco e commit/push su GitHub.
        remaining = int(pl.get("exp", 0) - time.time()) if pl else 0
        if jwt and 0 < remaining < TOKEN_REFRESH_THRESHOLD:
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
                        # Il push su GitHub blocca la risposta HTTP: va in background.
                        _push_token_in_background(target_p)
            except Exception as e_ref:
                console.print(f"[dim]  -> Avviso RefreshAccessToken: {e_ref}[/dim]")

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

    @staticmethod
    async def _post_license(sender, la_url, chal, did_variants, user_agents, header_fn, timeout):
        """POST della challenge al server licenze via curl o APIRequestContext.

        Un solo posto per i due trasporti: response.status (APIRequestContext) e
        response.status_code (curl) vanno normalizzati, e il timeout e' in ms
        nel primo caso e in secondi nel secondo.

        Ritorna {"ok": True, "body": <base64>} oppure l'ultimo errore.
        """
        import base64 as _b64
        last = None
        for lic_ua in user_agents:
            if not lic_ua:
                continue
            for cand_id in did_variants:
                headers = header_fn(cand_id, lic_ua)
                try:
                    resp = await sender.post(la_url, headers=headers, data=chal, timeout=timeout)
                    # APIRequestContext espone .status, curl espone .status_code
                    status = getattr(resp, "status", None)
                    if status is None:
                        status = getattr(resp, "status_code", 0)
                    if status == 200:
                        body = await resp.body() if hasattr(resp, "body") else resp.content
                        return {"ok": True, "body": _b64.b64encode(body).decode("ascii")}
                    text = await resp.text() if hasattr(resp, "text") else ""
                    hdrs = dict(getattr(resp, "headers", {}) or {})
                    last = {"ok": False, "status": status, "bodyText": text, "headers": hdrs,
                            "browser_res": last}
                except Exception as ex:
                    last = {"ok": False, "error": str(ex), "browser_res": last}
            if last and last.get("ok"):
                return last
        return last or {"ok": False, "error": "nessun tentativo eseguito"}

    async def _browser_request_context(self, only_if_running=False):
        """APIRequestContext del browser SENZA navigare su dazn.com.

        Serve al server licenze, che rifiuta (403 "Device not allowed") le
        richieste senza i cookie di sessione registrati nel profilo. Non
        serve pero' aprire la home: page.request e' legato al contesto, non
        alla pagina, quindi si paga solo l'avvio del contesto e non i ~25s di
        navigazione + sleep che fa ensure_session().

        only_if_running=True non avvia nulla: restituisce None se il contesto
        non e' gia' vivo, cosi da non pagare un launch solo per una prova.
        """
        from dazn_navigator2.services import browser as _brmod
        from dazn_navigator2.services.browser import get_browser
        if only_if_running:
            inst = getattr(_brmod, "_browser_instance", None)
            if inst is None or inst.context is None:
                return None
            return inst.context.request
        try:
            b = await get_browser(user_data_dir=self._profile_dir)
            if b is not None and b.context is not None:
                return b.context.request
        except Exception as e:
            console.print(f"[dim]  -> Contesto browser non disponibile: {e}[/dim]")
        return None

    async def estrai(self, profile_dir, asset_id, titolo="") -> dict:
        """Estrazione veloce: profilo su disco, senza browser se il token e' valido.

        Se il percorso rapido fallisce e il browser non e' mai stato avviato, il
        tentativo viene ripetuto usando il browser: il fallback esiste, ma
        costa ~30s e quindi viene eseguito solo quando serve davvero.
        """
        res = await self._estrai_core(profile_dir, asset_id, titolo, force_browser=False)
        if res.get("ok") or getattr(self, "_browser_used", False):
            return res
        console.print("[dim]  -> Percorso rapido fallito, retry con browser...[/dim]")
        return await self._estrai_core(profile_dir, asset_id, titolo, force_browser=True)

    async def _estrai_core(self, profile_dir, asset_id, titolo="", force_browser=False) -> dict:
        """Estrae MPD, PSSH, licenza e chiavi in modo istantaneo."""
        global _CACHED_SERVICES, _CACHED_CDM
        self.result = {"ok": False, "mpd_url": None, "pssh": None, "keys": None, "ua": None, "error": None}

        if not WVD_PATH:
            self.result["error"] = "File .wvd non trovato."
            return self.result

        import time
        _t = time.time()
        page, jwt = await self._get_page_and_jwt(profile_dir, force_browser=force_browser)
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
            # L'UA reale inviato dalla sessione curl_cffi: il token CDN e' legato
            # esattamente a questo valore, un UA "quasi uguale" porta a HTTP 401.
            client = await _get_http_session()
            ua = await get_session_user_agent(client)

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
        chosen_ua = ""

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

            c_hdrs = _cdn_headers(ua, c_tok)
            c_ua_used = ua

            try:
                r_resp = await client.get(c_fetch_url, headers=c_hdrs, timeout=6)
                if r_resp.status_code == 200:
                    mpd_r = {"ok": True, "status": 200, "body": r_resp.text}
                elif page:
                    # Il browser usa il proprio User-Agent: se va a buon fine
                    # quello diventa l'UA da salvare nell'evento.
                    page_ua = ""
                    try:
                        page_ua = await page.evaluate("navigator.userAgent")
                    except Exception:
                        page_ua = ""
                    mpd_r = await page.evaluate(
                        """async ({url, token}) => {
                            try {
                                const r = await fetch(url, { headers: { "dazn-token": token } });
                                return { ok: r.ok, status: r.status, body: await r.text() };
                            } catch(e) { return { ok: false, error: e.message }; }
                        }""",
                        {"url": c_fetch_url, "token": c_tok}
                    )
                    if mpd_r.get("ok"):
                        c_ua_used = page_ua or ua
                else:
                    mpd_r = {"ok": False, "status": r_resp.status_code, "body": r_resp.text}
            except Exception as e:
                mpd_r = {"ok": False, "error": str(e)}

            if mpd_r.get("ok"):
                chosen_pbd = cand_pbd
                chosen_mpd_url = c_mpd
                chosen_la_url = c_la
                chosen_token = c_tok
                chosen_cdn_name = c_name
                chosen_fetch_url = c_fetch_url
                chosen_ua = c_ua_used
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

        # Verifica finale: l'UA salvato deve essere quello accettato dalla CDN per
        # questo manifest (protegge da token generati con un profilo TLS diverso).
        ua = chosen_ua or ua
        verified_ua = validate_cdn_user_agent(fetch_mpd_url, dazn_token, [ua] + ua_candidates())
        if verified_ua:
            if verified_ua != ua:
                console.print(f"[dim]  -> UA verificato dalla CDN: Chrome/{''.join(c for c in verified_ua.split('Chrome/')[-1] if c.isdigit())[:3]}[/dim]")
            ua = verified_ua
        else:
            console.print("[yellow]  -> Attenzione: nessun User-Agent candidato accettato dalla CDN[/yellow]")

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

        def _lic_headers(cand_id, lic_ua):
            return {
                "content-type": "application/octet-stream",
                "user-agent": lic_ua,
                "authorization": f"Bearer {jwt}",
                "dazn-token": dazn_token,
                "x-dazn-token": dazn_token,
                "x-brand": "DAZN",
                "x-daznid": cand_id,
                "x-dazn-device": cand_id,
                "x-correlation-id": str(_uuid.uuid4()),
            }

        # ─── Trasporto per la licenza ───
        # La licenza DRM e' legata al device registrato nella sessione: senza
        # i cookie del profilo il server risponde 403 "Device not allowed".
        # Se il contesto del browser e' gia' vivo si va diretti li (gratis);
        # altrimenti si prova prima curl (veloce) e il contesto si avvia solo
        # se curl non basta.
        req_live = await self._browser_request_context(only_if_running=True)
        if req_live is not None:
            lr = await self._post_license(req_live, la_url, chal, did_variants, (ua, ANDROID_PLAYER_UA), _lic_headers, 10000)
        else:
            lr = await self._post_license(client, la_url, chal, did_variants, (ua,), _lic_headers, 10)
            if not (lr and lr.get("ok")):
                req = await self._browser_request_context()
                if req is not None:
                    lr = await self._post_license(req, la_url, chal, did_variants, (ua, ANDROID_PLAYER_UA), _lic_headers, 10000)

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

        # L'UA non deve MAI essere vuoto: l'addon lo ripropone a Kodi nelle
        # manifest_headers/stream_headers e senza quello la CDN risponde 401
        # su manifest e segmenti.
        if not _valid_browser_ua(ua):
            ua = detect_user_agent()

        hdrs_b64 = base64.b64encode(json.dumps({
            "user-agent": ua,
            "referer": "https://www.dazn.com/",
            "origin": "https://www.dazn.com",
            "dazn-token": dazn_token,
        }).encode("utf-8")).decode("utf-8")

        ext_mpd = chosen_fetch_url if chosen_fetch_url else mpd_url_original

        self.result["mpd"] = ext_mpd
        self.result["kodi_url"] = f"{fetch_mpd_url}&ck={ck}&headers={hdrs_b64}"

        self.result["dazn_token"] = dazn_token
        self.result["jwt"] = jwt
        self.result["cdn_name"] = cdn_name
        self.result["ua"] = ua
        # Blocco pronto per Kodi: inputstream.adaptive rifiuta license_key per
        # ClearKey, la proprieta' corretta e' drm_legacy.
        self.result["drm_legacy"] = "org.w3.clearkey|" + keys[0].replace("|", ",")
        self.result["headers"] = {
            "user-agent": ua,
            "referer": "https://www.dazn.com/",
            "origin": "https://www.dazn.com",
            "dazn-token": dazn_token,
        }

        self.result["ok"] = True

        self.result["titolo"] = titolo

        return self.result







