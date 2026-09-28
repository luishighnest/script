import asyncio, json, os, subprocess, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CDP_PORT = 9222

_current_profile_dir = BASE_DIR / "saved_profiles" / "profile_mpd" / "chrome_profile"

def set_active_profile_dir(p: Path):
    global _current_profile_dir
    _current_profile_dir = Path(p)

def get_active_profile_dir() -> Path:
    return _current_profile_dir

class BrowserManager:
    def __init__(self):
        self._context = None
        self._page = None
        self._playwright = None
        self._user_data_dir = None

    @property
    def page(self):
        return self._page

    @property
    def context(self):
        """Contesto del browser.

        Esposto perche' context.request e' un APIRequestContext che ri usa i
        cookie di sessione senza dover navigare su dazn.com: serve al server
        licenze DRM, che risponde 403 "Device not allowed" senza quei cookie.
        """
        return self._context

    async def start(self, user_data_dir: Path = None):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        
        p_dir = Path(user_data_dir) if user_data_dir else Path(get_active_profile_dir())
        p_dir.mkdir(parents=True, exist_ok=True)
        self._user_data_dir = p_dir

        launch_args = [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
            '--no-first-run'
        ]

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(p_dir),
                channel="msedge",
                headless=True,
                viewport={"width": 1280, "height": 720},
                args=launch_args
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            return
        except Exception:
            pass

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(p_dir),
                headless=True,
                viewport={"width": 1280, "height": 720},
                args=launch_args
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            return
        except Exception:
            pass

        browser = await self._try_connect_cdp()
        if browser is None:
            browser = await self._launch_chrome(p_dir)
            if browser is None:
                raise RuntimeError("Impossibile avviare o connettersi a Chrome.")
        self._context = browser.contexts[0]
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def _try_connect_cdp(self):
        try:
            browser = await self._playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{CDP_PORT}')
            return browser
        except Exception:
            return None

    async def _launch_chrome(self, profile_dir: Path):
        chrome_bin = "chromium"
        if sys.platform == "win32":
            chrome_bin = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
            if not os.path.exists(chrome_bin):
                chrome_bin = "msedge"
        try:
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                [chrome_bin, '--headless=new', f'--remote-debugging-port={CDP_PORT}',
                 f'--user-data-dir={profile_dir}', '--no-first-run', '--no-sandbox',
                 '--log-level=3', '--disable-logging',
                 '--disable-blink-features=AutomationControlled', 'about:blank'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=no_win
            )
            for _ in range(20):
                await asyncio.sleep(1)
                browser = await self._try_connect_cdp()
                if browser:
                    return browser
            return None
        except Exception:
            return None

    async def ensure_session(self):
        if self._page is None:
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
        try:
            cur = self._page.url
        except Exception:
            self._page = await self._context.new_page()
            cur = "about:blank"

        js_check = """
        (() => {
            try {
                const tok = window.localStorage ? window.localStorage.getItem('MISL.authToken') : null;
                if (!tok) return false;
                let p = tok.split('.')[1];
                p = p.replace(/-/g, '+').replace(/_/g, '/');
                while (p.length % 4) p += '=';
                const dec = JSON.parse(atob(p));
                return (dec.exp && dec.exp > Math.floor(Date.now() / 1000) + 300);
            } catch(e) { return false; }
        })()
        """

        if "dazn.com" in cur:
            try:
                if await self._page.evaluate(js_check):
                    return
            except Exception:
                pass

        try:
            await self._page.goto("https://www.dazn.com/it-IT/home", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
        except Exception:
            return

        try:
            is_valid = await self._page.evaluate(js_check)
        except Exception:
            is_valid = False

        if not is_valid:
            try:
                await self._page.goto("https://www.dazn.com/it-IT/home", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
            except Exception:
                pass

    async def evaluate(self, js: str):
        await self.ensure_session()
        if self._page is None:
            pages = self._context.pages if self._context else []
            self._page = pages[0] if pages else await self._context.new_page()
        safe_js = f"""
        (() => {{
            try {{
                return (eval({json.dumps(js)}));
            }} catch(e) {{
                return null;
            }}
        }})()
        """
        for attempt in range(3):
            try:
                return await self._page.evaluate(safe_js)
            except Exception as e:
                err_str = str(e).lower()
                if "execution context was destroyed" in err_str or "target closed" in err_str or "navigation" in err_str:
                    await asyncio.sleep(0.5)
                    if self._context:
                        pages = self._context.pages
                        if pages:
                            self._page = pages[0]
                    continue
                return None
        return None

    async def fetch_json(self, url: str, method: str = "GET", body: dict = None, headers: dict = None) -> dict:
        h = dict(headers or {})
        h.setdefault("Accept", "application/json")
        if body:
            h.setdefault("Content-Type", "application/json")
        hdrs = json.dumps(h)
        body_js = json.dumps(body) if body else "null"
        method_js = json.dumps(method)
        js = f"""
        async () => {{
            try {{
                const opts = {{ method: {method_js}, headers: {hdrs}, credentials: 'include' }};
                if ({body_js} !== null) opts.body = JSON.stringify({body_js});
                const r = await fetch({json.dumps(url)}, opts);
                if (!r.ok) return {{ ok: false, status: r.status }};
                const text = await r.text();
                try {{ return {{ ok: true, data: JSON.parse(text) }}; }}
                catch(e) {{ return {{ ok: true, text: text }}; }}
            }} catch(e) {{
                return {{ ok: false, error: e.message }};
            }}
        }}
        """
        res = await self.evaluate(js)
        if res and isinstance(res, dict):
            return res
        return {"ok": False, "error": "Chiamata fallita."}

    async def close(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._playwright = None

_browser_instance = None

# Lock di avvio, uno per event loop: il warmup del server e l'estrazione
# possono chiedere il browser insieme e senza lock creerebbero due istanze
# (o leggerebbero _user_data_dir prima che esista).
_start_locks = {}


def _get_start_lock():
    loop = asyncio.get_running_loop()
    lk = _start_locks.get(id(loop))
    if lk is None:
        lk = asyncio.Lock()
        _start_locks[id(loop)] = lk
    return lk


async def get_browser(user_data_dir: Path = None) -> BrowserManager:
    global _browser_instance
    async with _get_start_lock():
        if user_data_dir:
            set_active_profile_dir(user_data_dir)
            # Il contesto porta i cookie di sessione del profilo: riusarne uno di
            # un altro profilo fa fallire il server licenze (403 "Device not
            # allowed"). Se il profilo richiesto e' diverso si riavvia.
            if _browser_instance is not None and _browser_instance._user_data_dir is not None:
                if Path(_browser_instance._user_data_dir) != Path(user_data_dir):
                    try:
                        await _browser_instance.close()
                    except Exception:
                        pass
                    _browser_instance = None
        if _browser_instance is None:
            inst = BrowserManager()
            try:
                await inst.start(user_data_dir=user_data_dir)
            except Exception:
                # Un'istanza mezza avviata (contesto None) farebbe fallire
                # ogni richiesta successiva: si butta via e si ripropora' dopo.
                try:
                    await inst.close()
                except Exception:
                    pass
                raise
            _browser_instance = inst
        return _browser_instance
