import asyncio
import json
import os
import sys
import shutil
import time
import zipfile
import subprocess
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response, session, redirect, url_for

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Protezione per esecuzione nascosta/senza console (es. pythonw o script VBS)
if sys.stdout is None:
    try:
        sys.stdout = open(BASE_DIR / "flask.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        sys.stdout = io.StringIO()
if sys.stderr is None:
    try:
        sys.stderr = open(BASE_DIR / "flask.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        sys.stderr = io.StringIO()

import threading
from dazn_navigator2.cli.eventi_cmds import _load, _save, _fetch_from_upstash, add_event, pubblica
from dazn_navigator2.services.explorer import DaznExplorer
from dazn_navigator2.services.extractor import HeadlessExtractor
from dazn_navigator2.services.browser import BrowserManager

# Event loop persistente su thread dedicato per evitare 'Future attached to a different loop'
_ASYNC_LOOP = None
_ASYNC_THREAD = None
_LOOP_LOCK = threading.Lock()

# Cache dei VOD (solo sezione VOD): evita di rifare le ~70 chiamate DAZN a ogni click
_VOD_CACHE = None
_VOD_CACHE_TIME = 0
_VOD_CACHE_TTL = 300
_VOD_CACHE_LOCK = threading.Lock()

def _start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def run_async(coro, timeout=90):
    global _ASYNC_LOOP, _ASYNC_THREAD
    with _LOOP_LOCK:
        if _ASYNC_LOOP is None or not _ASYNC_LOOP.is_running():
            _ASYNC_LOOP = asyncio.new_event_loop()
            _ASYNC_THREAD = threading.Thread(target=_start_background_loop, args=(_ASYNC_LOOP,), daemon=True)
            _ASYNC_THREAD.start()
    future = asyncio.run_coroutine_threadsafe(coro, _ASYNC_LOOP)
    return future.result(timeout=timeout)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dazn-secret-auth-key-2026")

PROFILES_CONFIG_FILE = BASE_DIR / "profiles_config.json"
UPLOAD_PROFILES_DIR = BASE_DIR / "saved_profiles"
UPLOAD_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

# Profili con password dedicate
PROFILES = {
    "pz8": {"id": "pz8", "name": "Profilo PZ8"},
    "prova": {"id": "prova", "name": "Profilo Test"},
    "mpd": {"id": "mpd", "name": "Profilo MPD"}
}

def load_profiles_config():
    if PROFILES_CONFIG_FILE.exists():
        try:
            return json.loads(PROFILES_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_profiles_config(data):
    PROFILES_CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def sync_to_github(commit_msg: str):
    # Modalità 100% Locale: nessun push verso GitHub
    return True, "100% Locale (nessun sync remoto)"

def _image_url(img) -> str:
    if isinstance(img, dict):
        img_id = img.get("Id", "")
        if img_id:
            return f"https://image.discovery.indazn.com/eu/v3/eu/none/{img_id}/fill/none/top/none/100/1280/720/png/image"
        return ""
    return str(img) if img else ""

def _build_mpd_auth(mpd_url: str, dazn_token: str, cdn_name: str = "dazn-token") -> str:
    """Inserisce il token nel formato corretto:
    - /@token/ nel path per i token JWT dei canali lineari (es. indazn.com)
    - query parameter ?dazn-token=... per i token Akamai (tend:...) degli eventi Live e VOD (es. daznedge.net)
    """
    if not dazn_token or not mpd_url:
        return mpd_url
    if "/@" in mpd_url or "dazn-token=" in mpd_url or "hdnea=" in mpd_url:
        return mpd_url

    if dazn_token.startswith("eyJ") and "daznedge.net" not in mpd_url:
        if "://" in mpd_url:
            proto, rest = mpd_url.split("://", 1)
            if "/" in rest:
                host, path = rest.split("/", 1)
                return f"{proto}://{host}/@{dazn_token}/{path}"
            return f"{proto}://{rest}/@{dazn_token}"
        return mpd_url
    else:
        param_name = cdn_name if cdn_name else "dazn-token"
        sep = "&" if "?" in mpd_url else "?"
        return f"{mpd_url}{sep}{param_name}={dazn_token}"

def _format_tile_item(t):
    raw = getattr(t, 'raw', {}) or {}
    sport = raw.get("Sport", {})
    if isinstance(sport, dict):
        sport = sport.get("Title", "")
    elif sport:
        sport = str(sport)
    else:
        sport = ""

    comp = raw.get("Competition", {})
    if isinstance(comp, dict):
        comp = comp.get("Title", "")
    elif comp:
        comp = str(comp)
    else:
        comp = ""

    # Se la competizione non è specificata ed è un canale lineare o live tv
    ttype = getattr(t, 'tile_type', '') or ''
    if not comp:
        if ttype.lower() == 'linear' or 'dazn' in (getattr(t, 'title', '') or '').lower() or 'eurosport' in (getattr(t, 'title', '') or '').lower():
            comp = "Live TV"
        elif sport:
            comp = sport

    title_str = getattr(t, 'title', '') or ''
    if title_str.strip().upper() == "DAZN":
        title_str = "DAZN 1"
    elif title_str.strip().upper() == "EUROSPORT":
        title_str = "Eurosport 1"

    return {
        "id": t.id,
        "asset_id": getattr(t, 'asset_id', None) or t.id,
        "title": title_str,
        "sport": sport,
        "competition": comp or "Eventi",
        "image": _image_url(t.image),
        "tile_type": t.tile_type,
        "start": raw.get("Start") or "",
        "end": raw.get("End") or "",
    }

def _resolve_profile_dir(path_str):
    if not path_str:
        return None
    # Supporta sia separatori Windows che Linux
    clean_p = str(path_str).replace("\\", "/")
    p_obj = Path(clean_p)
    if not p_obj.is_absolute():
        p_obj = BASE_DIR / p_obj
    return p_obj

def get_active_chrome_profile(profile_id):
    # 1. Priorità assoluta: cartella chrome_profile presente direttamente sul Desktop
    desktop_profile = Path(r"C:\Users\alecl\Desktop\chrome_profile")
    if desktop_profile.exists():
        return str(desktop_profile)

    cfg = load_profiles_config()
    p = cfg.get(profile_id, {}).get("chrome_profile_path")
    if p:
        p_obj = _resolve_profile_dir(p)
        if p_obj and p_obj.exists():
            return str(p_obj)
    # Fallback predefinito alla cartella salvata per id
    fallback_dir = UPLOAD_PROFILES_DIR / f"profile_{profile_id}"
    if fallback_dir.exists():
        return str(fallback_dir)
    return str(BASE_DIR / "chrome_profile")

def _current_pid():
    return session.get("user_profile_id")

@app.route("/")
def home():
    if "user_profile_id" in session:
        return redirect("/script")
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        if password in PROFILES:
            prof = PROFILES[password]
            session["user_profile_id"] = prof["id"]
            session["user_profile_name"] = prof["name"]
            return redirect("/script")
        return render_template("login.html", error="Password non valida. Riprova.")
    
    if "user_profile_id" in session:
        return redirect("/script")
    return render_template("login.html")

@app.route("/script")
def script_page():
    if "user_profile_id" not in session:
        return redirect("/login")

    pid = session["user_profile_id"]
    pname = session.get("user_profile_name", "Profilo")
    cfg = load_profiles_config()
    saved_path = cfg.get(pid, {}).get("chrome_profile_path")
    
    has_folder = False
    if saved_path:
        p_obj = _resolve_profile_dir(saved_path)
        has_folder = p_obj.exists() and any(p_obj.iterdir()) if p_obj and p_obj.exists() else False
    
    if not has_folder:
        fallback_dir = UPLOAD_PROFILES_DIR / f"profile_{pid}"
        if fallback_dir.exists() and any(fallback_dir.iterdir()):
            has_folder = True
            saved_path = str(fallback_dir.relative_to(BASE_DIR)).replace("\\", "/")

    return render_template(
        "script.html",
        profile_name=pname,
        chrome_profile_set=has_folder,
        chrome_profile_path=saved_path or "Nessun profilo caricato"
    )

@app.route("/logout")
def logout_action():
    session.clear()
    return redirect("/login")

def _robust_remove_dir(path: Path):
    """Chiude prima i browser e rimuove la cartella del profilo in modo sicuro su Windows."""
    try:
        from dazn_navigator2.services.browser import close_browser
        run_async(close_browser())
    except Exception:
        pass

    import time, stat

    def remove_readonly(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if path.exists():
        for f_name in ["dazn_session.json", "auth_token.json"]:
            f_p = path / f_name
            if f_p.exists():
                try:
                    f_p.unlink()
                except Exception:
                    pass

        try:
            shutil.rmtree(path, onerror=remove_readonly)
        except Exception:
            time.sleep(0.3)
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

# API PER UPLOAD DAZN_SESSION.JSON / JSON SESSION CON AUTO-PUSH SU GITHUB
@app.route("/api/upload-profile-zip", methods=["POST"])
def upload_profile_zip():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Nessun file inviato"}), 400

    uploaded_file = request.files["file"]
    fname = (uploaded_file.filename or "").lower()
    if not fname.endswith(".json") and not fname.endswith(".zip"):
        return jsonify({"ok": False, "error": "Seleziona il file dazn_session.json o auth_token.json"}), 400

    pid = session["user_profile_id"]
    profile_dest = UPLOAD_PROFILES_DIR / f"profile_{pid}"

    if profile_dest.exists():
        _robust_remove_dir(profile_dest)
    profile_dest.mkdir(parents=True, exist_ok=True)

    if fname.endswith(".zip"):
        zip_path = profile_dest / "temp_profile.zip"
        uploaded_file.save(str(zip_path))
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(profile_dest)
            zip_path.unlink(missing_ok=True)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Errore durante l'estrazione dello zip: {e}"}), 500
    else:
        # File JSON diretto (dazn_session.json o auth_token.json)
        dest_json = profile_dest / "dazn_session.json"
        uploaded_file.save(str(dest_json))
        try:
            data = json.loads(dest_json.read_text(encoding="utf-8"))
            tok = data.get("jwt")
            if tok:
                (profile_dest / "auth_token.json").write_text(json.dumps({"jwt": tok}), encoding="utf-8")
        except Exception:
            pass

    rel_profile_str = str(profile_dest.relative_to(BASE_DIR)).replace("\\", "/")
    cfg = load_profiles_config()
    if pid not in cfg:
        cfg[pid] = {}
    cfg[pid]["chrome_profile_path"] = rel_profile_str
    save_profiles_config(cfg)

    # SINCRONIZZA AUTOMATICAMENTE SU GITHUB PERMANENTEMENTE
    git_ok, git_msg = sync_to_github(f"persist: aggiorna sessione dazn per {pid}")

    return jsonify({
        "ok": True,
        "profile": session.get("user_profile_name", pid),
        "chrome_profile_path": rel_profile_str,
        "github_sync": git_ok,
        "github_msg": git_msg
    })

@app.route("/api/delete-profile-session", methods=["POST"])
def delete_profile_session():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401

    pid = session["user_profile_id"]
    profile_dest = UPLOAD_PROFILES_DIR / f"profile_{pid}"

    if profile_dest.exists():
        _robust_remove_dir(profile_dest)

    cfg = load_profiles_config()
    if pid in cfg:
        cfg[pid]["chrome_profile_path"] = ""
        save_profiles_config(cfg)

    git_ok, git_msg = sync_to_github(f"persist: elimina sessione dazn per {pid}")

    return jsonify({
        "ok": True,
        "message": "Sessione del profilo eliminata con successo",
        "github_sync": git_ok,
        "github_msg": git_msg
    })

def refresh_all_dazn_sessions():
    """Rinnova automaticamente tutti i token JWT DAZN in background prima che scadano."""
    import requests
    import time

    cfg = load_profiles_config()
    refreshed_any = False

    for pid in list(PROFILES.keys()):
        profile_dir = Path(get_active_chrome_profile(pid))
        if not profile_dir.exists():
            continue

        session_file = profile_dir / "dazn_session.json"
        auth_file = profile_dir / "auth_token.json"

        if not session_file.exists() and not auth_file.exists():
            continue

        current_jwt = ""
        session_data = {}

        if session_file.exists():
            try:
                session_data = json.loads(session_file.read_text(encoding="utf-8"))
                current_jwt = session_data.get("jwt", "")
            except Exception:
                pass

        if not current_jwt and auth_file.exists():
            try:
                auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
                current_jwt = auth_data.get("jwt", "")
            except Exception:
                pass

        if not current_jwt or not current_jwt.startswith("eyJ"):
            continue

        try:
            # 1) Chiamata API ufficiale RefreshAccessToken DAZN
            res = requests.post(
                "https://ott-authz-bff-prod.ar.indazn.com/v5/RefreshAccessToken",
                headers={
                    "authorization": f"Bearer {current_jwt}",
                    "content-type": "application/json",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                },
                timeout=12
            )

            if res.status_code == 200:
                data = res.json()
                new_token = data.get("AuthToken", {}).get("Token")
                if new_token and new_token != current_jwt:
                    print(f"[Auto-Refresh DAZN] Token rinnovato con successo per il profilo {pid}!")
                    if session_file.exists():
                        session_data["jwt"] = new_token
                        session_data["created_at"] = int(time.time())
                        session_file.write_text(json.dumps(session_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    auth_file.write_text(json.dumps({"jwt": new_token}), encoding="utf-8")
                    refreshed_any = True
            elif res.status_code == 401:
                print(f"[Auto-Refresh DAZN] Token per {pid} scaduto. Tentativo rinnovo da cookies...")
                cookies = session_data.get("cookies", [])
                if cookies:
                    try:
                        c_dict = {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
                        r_rail = requests.get(
                            "https://stage-dazn-web-api.indazn.com/v2/rail?id=Home",
                            cookies=c_dict,
                            headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                            timeout=10
                        )
                        fresh_jwt = r_rail.headers.get("authorization") or r_rail.headers.get("dazn-token")
                        if fresh_jwt and fresh_jwt.startswith("Bearer "):
                            fresh_jwt = fresh_jwt.replace("Bearer ", "").strip()
                        if fresh_jwt and fresh_jwt.startswith("eyJ"):
                            print(f"[Auto-Refresh DAZN] Token ripristinato con successo dai cookies per {pid}!")
                            session_data["jwt"] = fresh_jwt
                            session_data["created_at"] = int(time.time())
                            session_file.write_text(json.dumps(session_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                            auth_file.write_text(json.dumps({"jwt": fresh_jwt}), encoding="utf-8")
                            refreshed_any = True
                    except Exception as e_c:
                        print(f"[Auto-Refresh DAZN] Errore rinnovo da cookies: {e_c}")
        except Exception as e:
            print(f"[Auto-Refresh DAZN Error] {pid}: {e}")

    if refreshed_any:
        sync_to_github("auto-refresh: rinnovo automatico token DAZN per i profili")

def _background_dazn_refresher():
    """Thread in sottofondo che esegue il rinnovo automatico ogni 45 minuti."""
    time.sleep(15)
    while True:
        try:
            refresh_all_dazn_sessions()
        except Exception as e:
            print(f"[Auto-Refresh Loop Error] {e}")
        time.sleep(2700) # Ogni 45 minuti

_refresher_thread = threading.Thread(target=_background_dazn_refresher, daemon=True)
_refresher_thread.start()

@app.route("/api/session-status", methods=["GET"])
def api_session_status():
    """Restituisce lo stato e la scadenza (exp) del token JWT DAZN del profilo attivo."""
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401

    pid = _current_pid()
    profile_dir = Path(get_active_chrome_profile(pid))
    session_file = profile_dir / "dazn_session.json"
    auth_file = profile_dir / "auth_token.json"

    jwt_token = ""
    if session_file.exists():
        try:
            sdata = json.loads(session_file.read_text(encoding="utf-8"))
            jwt_token = sdata.get("jwt", "")
        except Exception:
            pass

    if not jwt_token and auth_file.exists():
        try:
            adata = json.loads(auth_file.read_text(encoding="utf-8"))
            jwt_token = adata.get("jwt", "")
        except Exception:
            pass

    if not jwt_token or not jwt_token.startswith("eyJ"):
        return jsonify({"ok": False, "has_token": False, "exp": None, "remaining": 0})

    try:
        import base64
        parts = jwt_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            # Fix base64 padding
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload_bytes = base64.b64decode(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
            exp = payload.get("exp")
            if exp:
                remaining = max(0, int(exp - time.time()))
                return jsonify({
                    "ok": True,
                    "has_token": True,
                    "exp": exp,
                    "remaining": remaining,
                    "profile": pid
                })
    except Exception as e:
        print(f"[Session Status Error] {e}")

    return jsonify({"ok": True, "has_token": True, "exp": None, "remaining": None, "profile": pid})

@app.route("/api/refresh-dazn-session", methods=["POST"])
def api_refresh_dazn_session():
    """Endpoint per sollecitare il rinnovo manuale o via cron/webhook della sessione DAZN."""
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    try:
        refresh_all_dazn_sessions()
        return jsonify({"ok": True, "message": "Rinnovo sessione eseguito con successo!"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/events", methods=["GET"])
def get_saved_events():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401
    
    local_data = _load(_current_pid()) or {}
    try:
        import requests
        url = "https://ace-seal-162556.upstash.io/get/stream:eventi_mpd"
        headers = {"Authorization": "Bearer gQAAAAAAAnr8AAIgcDEyZjRkYjEwYmUzZDY0M2RhYjZkNjhmMDFjNGVkMjVmYw"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            raw = res.json().get("result")
            if raw and raw != "null":
                upstash_data = json.loads(raw)
                if isinstance(upstash_data, dict) and upstash_data:
                    merged = dict(upstash_data)
                    for comp, items in local_data.items():
                        if comp not in merged:
                            merged[comp] = items
                        else:
                            existing_names = {e.get("name") for e in merged[comp] if isinstance(e, dict)}
                            for item in items:
                                if isinstance(item, dict) and item.get("name") not in existing_names:
                                    merged[comp].append(item)
                    return jsonify(merged)
    except Exception as e:
        print(f"[Upstash API Error] {e}")
    return jsonify(local_data)

@app.route("/api/events/repair-ua", methods=["POST"])
def api_repair_event_ua():
    """Riapplica l'User-Agent accettato dalla CDN a tutti gli eventi salvati.

    Il token CDN e' legato all'UA: se un evento e' stato estratto con un altro
    profilo TLS la CDN risponde 401 e Kodi non avvia il video.
    """
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    from dazn_navigator2.cli.eventi_cmds import ripara_user_agent
    data, report = ripara_user_agent()
    return jsonify({
        "ok": True,
        "checked": len(report),
        "fixed": [
            {"comp": c, "name": n, "old": o, "new": v} for c, n, o, v, ch in report if ch
        ],
        "unresolved": [
            {"comp": c, "name": n, "old": o} for c, n, o, v, ch in report if not v
        ],
    })

@app.route("/api/events/rename", methods=["POST"])
def rename_saved_event():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    body = request.get_json() or {}
    comp = body.get("comp")
    index = body.get("index")
    new_name = (body.get("new_name") or "").strip()
    old_name = body.get("old_name")
    
    if not new_name:
        return jsonify({"ok": False, "error": "Parametri non validi"}), 400
    
    data = _fetch_from_upstash()
    renamed = False
    
    if comp and comp in data and index is not None:
        idx = int(index)
        if 0 <= idx < len(data[comp]):
            data[comp][idx]["name"] = new_name
            renamed = True

    if not renamed and old_name:
        for c, items in data.items():
            for ev in items:
                if ev.get("name") == old_name:
                    ev["name"] = new_name
                    renamed = True
                    break
            if renamed:
                break

    if renamed:
        pubblica(f"Rinomina evento {new_name}", data)
        sync_to_github(f"edit: rinomina evento {new_name} ({_current_pid()})")
        return jsonify({"ok": True})
        
    return jsonify({"ok": False, "error": "Evento non trovato"}), 404

@app.route("/api/events/delete", methods=["POST"])
def delete_saved_event():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    body = request.get_json() or {}
    if body.get("all"):
        pubblica("Cancellati tutti gli eventi", {})
        sync_to_github(f"edit: cancellati tutti gli eventi ({_current_pid()})")
        return jsonify({"ok": True})
    
    comp = body.get("comp")
    index = body.get("index")
    ev_name = body.get("name")
    
    data = _fetch_from_upstash()
    removed = False

    # 1. Tenta per comp + index
    if comp and comp in data and index is not None:
        idx = int(index)
        if 0 <= idx < len(data[comp]):
            del data[comp][idx]
            if not data[comp]:
                del data[comp]
            removed = True

    # 2. Fallback per nome dell'evento
    if not removed and ev_name:
        for c, items in list(data.items()):
            new_items = [e for e in items if e.get("name") != ev_name]
            if len(new_items) < len(items):
                if new_items:
                    data[c] = new_items
                else:
                    del data[c]
                removed = True
                break

    # 3. Fallback per comp case-insensitive
    if not removed and comp and index is not None:
        idx = int(index)
        for c in list(data.keys()):
            if c.strip().lower() == comp.strip().lower():
                if 0 <= idx < len(data[c]):
                    del data[c][idx]
                    if not data[c]:
                        del data[c]
                    removed = True
                    break

    if removed:
        pubblica("Rimosso evento", data)
        sync_to_github(f"edit: rimosso evento ({_current_pid()})")
        return jsonify({"ok": True})
        
    return jsonify({"ok": False, "error": "Evento non trovato"}), 404

@app.route("/api/events/sort", methods=["POST"])
def sort_saved_events():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    from dazn_navigator2.cli.eventi_cmds import _iter_entries, _sort_key
    data = _fetch_from_upstash()
    entries = list(_iter_entries(data))
    if not entries:
        return jsonify({"ok": True})
    
    ordinato = sorted(entries, key=_sort_key)
    nuovo_data = {}
    for _, comp, _, ev_ in ordinato:
        nuovo_data.setdefault(comp, []).append(ev_)
    pubblica("Eventi riordinati per data", nuovo_data)
    sync_to_github(f"edit: eventi riordinati per data ({_current_pid()})")
    return jsonify({"ok": True})

@app.route("/api/events/sync_next", methods=["POST"])
def sync_events_to_next():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401
    
    import re, unicodedata, requests
    
    pid = _current_pid()
    local_data = _load(pid)
    # Se il profilo corrente non ha eventi o è diverso da mpd, consideriamo anche dazn_event_mpd.json se esiste
    if not local_data and pid != "mpd":
        local_data = _load("mpd")
    
    if not local_data:
        return jsonify({"ok": False, "error": "Nessun evento estratto da sincronizzare"}), 400

    upstash_url = "https://ace-seal-162556.upstash.io"
    upstash_token = "gQAAAAAAAnr8AAIgcDEyZjRkYjEwYmUzZDY0M2RhYjZkNjhmMDFjNGVkMjVmYw"
    headers = {"Authorization": f"Bearer {upstash_token}"}
    
    try:
        r = requests.get(f"{upstash_url}/get/stream:eventi", headers=headers, timeout=10)
        res_json = r.json()
        raw_val = res_json.get("result")
        next_data = json.loads(raw_val) if raw_val else {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Errore lettura Next DB: {e}"}), 500

    def _normalize_title(s: str) -> str:
        if not s:
            return ""
        s = re.sub(r'[\(\[\{].*?[\)\]\}]', ' ', s) # rimuove (WARP), ecc.
        s = s.replace("vs.", " ").replace("vs", " ").replace("-", " ").replace("|", " ")
        s = unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('utf-8')
        words = sorted([w for w in re.sub(r'[^a-zA-Z0-9]', ' ', s).lower().split() if w])
        return " ".join(words)

    updated_count = 0
    added_count = 0

    for comp, items in local_data.items():
        if not isinstance(items, list):
            continue
        for ev in items:
            ev_name = ev.get("name", "").strip()
            norm_name = _normalize_title(ev_name)
            if not norm_name:
                continue

            matched_item = None
            
            # Cerca prima nella stessa competizione (o competizione simile)
            candidate_categories = [comp] if comp in next_data else []
            candidate_categories += [c for c in next_data.keys() if c != comp]

            for cat_key in candidate_categories:
                for target_ev in next_data.get(cat_key, []):
                    t_name = target_ev.get("name", "").strip()
                    norm_t = _normalize_title(t_name)
                    if not norm_t:
                        continue
                    
                    # Match esatto dei token oppure inclusione sicura
                    if norm_name == norm_t:
                        matched_item = target_ev
                        break
                    set_a = set(norm_name.split())
                    set_b = set(norm_t.split())
                    if len(set_a) >= 2 and set_a.issubset(set_b) or (len(set_b) >= 2 and set_b.issubset(set_a)):
                        matched_item = target_ev
                        break
                if matched_item:
                    break

            if matched_item:
                # AGGIORNA SOLO I PARAMETRI DELLO STREAM, PRESERVANDO IMMAGINE E NOME ESISTENTI!
                if ev.get("mpd"):
                    matched_item["mpd"] = ev["mpd"]
                    matched_item["url"] = ev["mpd"]
                if ev.get("key"):
                    matched_item["key"] = ev["key"]
                    matched_item["kid_key"] = ev["key"]
                if ev.get("ua"):
                    matched_item["ua"] = ev["ua"]
                if ev.get("start") and not matched_item.get("start"):
                    matched_item["start"] = ev["start"]
                if ev.get("end") and not matched_item.get("end"):
                    matched_item["end"] = ev["end"]
                updated_count += 1
            else:
                # Evento non trovato in Next: aggiungilo nella categoria corrispondente
                next_data.setdefault(comp, []).append(ev)
                added_count += 1

    # Salva il dizionario unificato su Upstash Redis stream:eventi
    try:
        payload = json.dumps(next_data, ensure_ascii=False)
        save_res = requests.post(f"{upstash_url}/set/stream:eventi", headers=headers, data=payload, timeout=10)
        if save_res.status_code != 200:
            return jsonify({"ok": False, "error": f"Errore scrittura Redis: {save_res.text}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Errore salvataggio Next DB: {e}"}), 500

    # Sincronizza anche localmente su next/public/test.json, htdocs/test.json e kodi_repo/test.json (AES-256-GCM)
    try:
        import os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.backends import default_backend
        import base64

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"zadonkais_secure_salt_2026",
            iterations=100000,
            backend=default_backend()
        )
        key = kdf.derive("2941".encode("utf-8"))
        aesgcm = AESGCM(key)
        iv = os.urandom(12)
        plaintext = json.dumps(next_data, ensure_ascii=False).encode("utf-8")
        ciphertext = aesgcm.encrypt(iv, plaintext, None)
        enc_payload = base64.b64encode(iv + ciphertext).decode("ascii")
        json_enc_text = json.dumps({"enc": enc_payload}, indent=2, ensure_ascii=False) + "\n"

        target_paths = [
            Path(r"C:\Users\alecl\Desktop\next\public\test.json"),
            Path(r"C:\Users\alecl\Desktop\htdocs\test.json"),
            Path(r"C:\Users\alecl\Desktop\kodi_repo\test.json")
        ]
        for tp in target_paths:
            if tp.parent.exists():
                try:
                    tp.write_text(json_enc_text, encoding="utf-8")
                except Exception:
                    pass
    except Exception as e:
        print(f"[Sync test.json Local Error] {e}")

    return jsonify({
        "ok": True,
        "updated": updated_count,
        "added": added_count,
        "message": f"Sincronizzazione completata: {updated_count} eventi aggiornati con chiavi/mpd, {added_count} nuovi aggiunti."
    })


@app.route("/api/live", methods=["GET"])
def get_live_events():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    async def _fetch():
        explorer = DaznExplorer()
        tiles = await explorer.get_tiles("Live")
        items = [_format_tile_item(t) for t in tiles]
        await explorer.close()
        return items

    try:
        data = run_async(_fetch(), timeout=45)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/vod", methods=["GET"])
def get_vod_events():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    async def _fetch():
        explorer = DaznExplorer()
        tiles = await explorer.get_tiles("Catchup")
        items = [_format_tile_item(t) for t in tiles]
        await explorer.close()
        return items

    try:
        data = run_async(_fetch(), timeout=45)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/vod/categories", methods=["GET"])
def get_vod_categories():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    global _VOD_CACHE, _VOD_CACHE_TIME
    with _VOD_CACHE_LOCK:
        if _VOD_CACHE is not None and (time.monotonic() - _VOD_CACHE_TIME) < _VOD_CACHE_TTL:
            return jsonify(_VOD_CACHE)

    async def _fetch():
        explorer = DaznExplorer()
        res = await explorer.get_vod_categories()
        await explorer.close()

        ultimi = [_format_tile_item(t) for t in res.get("ultimi", [])]

        categorie = {}
        for t in res.get("all", []):
            item = _format_tile_item(t)
            cname = item.get("competition") or "Altro"
            categorie.setdefault(cname, []).append(item)

        return {"ultimi": ultimi, "categorie": categorie}

    try:
        data = run_async(_fetch(), timeout=150)
        with _VOD_CACHE_LOCK:
            _VOD_CACHE = data
            _VOD_CACHE_TIME = time.monotonic()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/linear", methods=["GET"])
def get_linear_channels():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    async def _fetch():
        explorer = DaznExplorer()
        tiles = await explorer.get_tiles("epg")
        if not tiles:
            tiles = await explorer.get_tiles("LinearChannels")
        items = [_format_tile_item(t) for t in tiles]
        
        # Risolvi per ciascun canale il corrispettivo tile 'Live' da search per garantire l'AssetId abilitato
        for it in items:
            t_name = it.get("title", "").strip()
            try:
                s_res = await explorer.search(t_name)
                match = [x for x in s_res if x.tile_type == 'Live' and x.title.strip().lower() == t_name.lower()]
                if match:
                    it["asset_id"] = match[0].asset_id or match[0].id
                    if match[0].image:
                        it["image"] = _image_url(match[0].image)
                    if match[0].raw.get("Start"):
                        it["start"] = match[0].raw.get("Start")
                    if match[0].raw.get("End"):
                        it["end"] = match[0].raw.get("End")
            except Exception:
                pass

        await explorer.close()
        return items

    try:
        data = run_async(_fetch(), timeout=45)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search", methods=["GET"])
def search_events():
    if "user_profile_id" not in session:
        return jsonify({"error": "Non autenticato"}), 401

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    async def _fetch():
        explorer = DaznExplorer()
        tiles = await explorer.search(q)
        items = [_format_tile_item(t) for t in tiles]
        await explorer.close()
        return items

    try:
        data = run_async(_fetch(), timeout=45)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/diagnose")
def diagnose():
    import time as _time
    now = _time.time()
    from dazn_navigator2.services.extractor import _CACHED_SERVICES
    results = {
        "_playback_endpoint": _CACHED_SERVICES.get("Playback", ""),
    }
    for pid in PROFILES:
        profile_dir = Path(get_active_chrome_profile(pid))
        auth_file = profile_dir / "auth_token.json"
        info = {"profile_dir": str(profile_dir), "auth_file_exists": auth_file.exists()}
        if auth_file.exists():
            try:
                data = json.loads(auth_file.read_text(encoding="utf-8"))
                tok = data.get("jwt", "")
                ext = HeadlessExtractor.__new__(HeadlessExtractor)
                pl = ext._decode_jwt_payload(tok) if tok.startswith("eyJ") else None
                if pl:
                    remaining = int(pl.get("exp", 0) - now)
                    info["country"] = pl.get("country")
                    info["exp"] = pl.get("exp")
                    info["remaining_seconds"] = remaining
                    info["valid_it"] = pl.get("country") == "it" and remaining > 0
                    info["device_id"] = (pl.get("deviceId") or "")[:40]
                else:
                    info["token_parse_error"] = True
            except Exception as e:
                info["error"] = str(e)
        results[pid] = info
    return jsonify(results)

@app.route("/api/extract", methods=["POST"])
def extract_stream():
    if "user_profile_id" not in session:
        return jsonify({"ok": False, "error": "Non autenticato"}), 401

    pid = session["user_profile_id"]
    body = request.get_json() or {}
    asset_id = body.get("asset_id") or body.get("id")
    title = body.get("title", "Evento")
    image = body.get("image", "")
    competition = (body.get("competition") or "").strip()
    start = body.get("start") or ""
    end = body.get("end") or ""

    if not asset_id:
        return jsonify({"ok": False, "error": "asset_id mancante"}), 400

    target_profile_dir = get_active_chrome_profile(pid)

    async def _do_extract():
        nonlocal competition, start, end
        # Rilevamento automatico se si tratta di canale lineare
        title_lower = (title or "").strip().lower()
        is_linear_chan = any(k in title_lower for k in ("dazn 1", "dazn 2", "dazn 3", "dazn 4", "dazn 5", "eurosport", "zona dazn", "milan tv", "inter tv", "juve tv"))

        # Risoluzione metadati e categoria reale se mancante o generica
        if not start or not end or not competition or competition in ("Eventi Live", "Eventi"):
            if is_linear_chan:
                competition = "Live TV"
            else:
                try:
                    explorer = DaznExplorer()
                    s_res = await explorer.search(title)
                    for match_item in s_res:
                        raw = getattr(match_item, 'raw', {}) or {}
                        c_title = ""
                        comp_raw = raw.get("Competition")
                        if isinstance(comp_raw, dict):
                            c_title = comp_raw.get("Title") or ""
                        elif comp_raw:
                            c_title = str(comp_raw)

                        if c_title and c_title not in ("Eventi Live", "Eventi"):
                            competition = c_title
                            if not start and raw.get("Start"):
                                start = raw.get("Start")
                            if not end and raw.get("End"):
                                end = raw.get("End")
                            break

                    if not competition or competition in ("Eventi Live", "Eventi"):
                        for match_item in s_res:
                            raw = getattr(match_item, 'raw', {}) or {}
                            s_title = ""
                            sport_raw = raw.get("Sport")
                            if isinstance(sport_raw, dict):
                                s_title = sport_raw.get("Title") or ""
                            elif sport_raw:
                                s_title = str(sport_raw)
                            if s_title:
                                competition = s_title
                                break
                    await explorer.close()
                except Exception:
                    pass

        if not competition:
            competition = "Live TV" if is_linear_chan else "Eventi"

        ext = HeadlessExtractor()
        res = await ext.estrai(target_profile_dir, asset_id, title)
        if res.get("ok"):
            mpd_url = res.get("mpd_url", "")
            dazn_token = res.get("dazn_token", "")
            cdn_name = res.get("cdn_name", "dazn-token")
            mpd_auth = _build_mpd_auth(mpd_url, dazn_token, cdn_name)
            keys_str = ",".join(res.get("keys", []))
            from dazn_navigator2.services.extractor import detect_user_agent
            ua_str = (res.get("ua") or "").strip() or detect_user_agent()
            logo = image or _image_url(res.get("image"))

            base_titolo = res.get("titolo") or title
            import re
            event_name = re.sub(r'\s*\(WARP\)\s*', ' ', base_titolo, flags=re.IGNORECASE).strip()
            if event_name.strip().upper() == "DAZN":
                event_name = "DAZN 1"
            elif event_name.strip().upper() == "EUROSPORT":
                event_name = "Eurosport 1"

            entry = {
                "name": event_name,
                "image": logo,
                "start": start,
                "end": end,
                "mpd": mpd_auth,
                "key": keys_str,
                "ua": ua_str
            }
            add_event(competition, entry, _current_pid())
            sync_to_github(f"extract: salvato evento {event_name} ({_current_pid()})")
            res["mpd_url"] = mpd_auth
            res["mpd_auth"] = mpd_auth
            res["entry"] = entry
            res["competition"] = competition
        return res

    try:
        result = run_async(_do_extract(), timeout=90)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/playlist.m3u", methods=["GET"])
def generate_m3u():
    from dazn_navigator2.services.extractor import detect_user_agent
    default_ua = detect_user_agent()
    data = _load(_current_pid())
    lines = ["#EXTM3U"]
    for comp, items in data.items():
        for ev in items:
            name = ev.get("name", "Evento")
            logo = ev.get("logo") or ev.get("image", "")
            mpd = ev.get("mpd") or ev.get("manifest", "")
            keys = ev.get("key") or ev.get("keys", "")
            ua = (ev.get("ua") or "").strip() or default_ua

            props = f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo}" group-title="{comp}",{name}'
            if keys:
                # ClearKey: inputstream.adaptive accetta solo drm_legacy
                # (license_key viene rifiutata -> nessuna chiave -> niente video)
                lines.append(f'#KODIPROP:inputstream.adaptive.drm_legacy=org.w3.clearkey|{keys.replace("|", ",")}')
            hdrs = f'User-Agent={ua}&Referer=https://www.dazn.com/&Origin=https://www.dazn.com'
            lines.append(f'#KODIPROP:inputstream.adaptive.manifest_headers={hdrs}')
            lines.append(f'#KODIPROP:inputstream.adaptive.stream_headers={hdrs}')
            lines.append(props)
            lines.append(mpd)
    
    return Response("\n".join(lines), mimetype="audio/x-mpegurl")

def _auto_extract_worker():
    """Scansiona e estrae automaticamente e istantaneamente tutti gli eventi Live delle competizioni target."""
    import time
    from datetime import datetime, timezone
    
    TARGET_COMPETITIONS = {
        "serie a enilive",
        "serie bkt",
        "laliga ea sports"
    }
    
    time.sleep(5)  # Attende l'avvio completo del server
    print("[AutoExtract] Servizio estrazione automatica competizioni attivo (Serie A, Serie B, LaLiga).")
    
    while True:
        try:
            async def _check_and_extract():
                pid = "mpd"  # Salva direttamente nel profilo mpd (sincronizzato con stream:eventi_mpd)
                target_profile_dir = get_active_chrome_profile(pid)
                if not target_profile_dir:
                    return
                
                # Leggi eventi gia' estratti
                current_data = _load(pid)
                extracted_names = set()
                for comp, ev_list in current_data.items():
                    for ev in ev_list:
                        n = (ev.get("name") or "").strip().lower()
                        if n and (ev.get("mpd") or ev.get("manifest")):
                            extracted_names.add(n)
                
                explorer = DaznExplorer()
                # Cerca contenuti live
                tiles = []
                for query in ("Serie A", "Serie B", "LaLiga", "Live"):
                    try:
                        res = await explorer.search(query)
                        tiles.extend(res)
                    except Exception:
                        pass
                
                now = datetime.now(timezone.utc)
                candidates = []
                seen_assets = set()
                
                for t in tiles:
                    asset_id = t.asset_id or t.id
                    if not asset_id or asset_id in seen_assets:
                        continue
                    seen_assets.add(asset_id)
                    
                    raw = getattr(t, "raw", {}) or {}
                    comp_raw = raw.get("Competition", {})
                    c_title = comp_raw.get("Title") if isinstance(comp_raw, dict) else str(comp_raw or "")
                    c_title_clean = c_title.strip().lower()
                    
                    if c_title_clean not in TARGET_COMPETITIONS:
                        continue
                    
                    # Controlla se e' in corso / live
                    start_str = raw.get("Start") or ""
                    end_str = raw.get("End") or ""
                    is_live = False
                    
                    if t.tile_type == "Live":
                        is_live = True
                    elif start_str:
                        try:
                            st_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                            if end_str:
                                en_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                                is_live = (st_dt <= now <= en_dt)
                            else:
                                # Se iniziato da meno di 3 ore
                                is_live = (st_dt <= now and (now - st_dt).total_seconds() < 10800)
                        except Exception:
                            pass
                    
                    if not is_live:
                        continue
                    
                    # Verifica se e' gia' stato estratto
                    ev_title = t.title.strip()
                    import re
                    clean_title = re.sub(r'\s*\(WARP\)\s*', ' ', ev_title, flags=re.IGNORECASE).strip()
                    if clean_title.lower() in extracted_names:
                        continue
                    
                    candidates.append({
                        "asset_id": asset_id,
                        "title": clean_title,
                        "competition": c_title,
                        "image": _image_url(t.image),
                        "start": start_str,
                        "end": end_str
                    })
                
                await explorer.close()
                
                # Estrai ciascun evento mancante
                for cand in candidates:
                    print(f"[AutoExtract] Rilevato evento LIVE da estrarre: {cand['title']} ({cand['competition']})")
                    try:
                        ext = HeadlessExtractor()
                        res = await ext.estrai(target_profile_dir, cand["asset_id"], cand["title"])
                        if res.get("ok"):
                            mpd_url = res.get("mpd_url", "")
                            dazn_token = res.get("dazn_token", "")
                            mpd_auth = _build_mpd_auth(mpd_url, dazn_token)
                            keys_str = ",".join(res.get("keys", []))
                            from dazn_navigator2.services.extractor import detect_user_agent
                            ua_str = (res.get("ua") or "").strip() or detect_user_agent()
                            logo = cand["image"] or _image_url(res.get("image"))
                            
                            entry = {
                                "name": cand["title"],
                                "image": logo,
                                "start": cand["start"],
                                "end": cand["end"],
                                "mpd": mpd_auth,
                                "key": keys_str,
                                "ua": ua_str
                            }
                            add_event(cand["competition"], entry, pid)
                            sync_to_github(f"auto-extract: {cand['title']} ({cand['competition']})")
                            print(f"[AutoExtract] Estratto e sincronizzato con successo: {cand['title']}")
                            extracted_names.add(cand["title"].lower())
                    except Exception as err:
                        print(f"[AutoExtract Error] Errore estrazione {cand['title']}: {err}")
                    time.sleep(2)
            
            run_async(_check_and_extract(), timeout=180)
        except Exception as e:
            pass
        
        time.sleep(45)  # Riesegue il controllo ogni 45 secondi

# Avvia il worker in background
_auto_thread = threading.Thread(target=_auto_extract_worker, daemon=True)
_auto_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
