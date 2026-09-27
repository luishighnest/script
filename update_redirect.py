"""Aggiorna la pagina GitHub Pages che reindirizza al link tunnel corrente in tempo reale."""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INDEX = BASE_DIR / "index.html"
TOKEN_FILE = BASE_DIR / "github_token.txt"

# Protezione per esecuzione con pythonw.exe (senza console/stdout)
if sys.stdout is None:
    try:
        sys.stdout = open(BASE_DIR / "redirect.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        sys.stdout = io.StringIO()
if sys.stderr is None:
    try:
        sys.stderr = open(BASE_DIR / "redirect.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        import io
        sys.stderr = io.StringIO()

TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Script2</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate, max-age=0">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="0;url={link}">
<script>
    (function() {{
        var target = "{link}";
        if (target && target.startsWith("http")) {{
            window.location.replace(target);
        }}
    }})();
</script>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0f14;
    color: #e2e8f0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    margin: 0;
    text-align: center;
}}
.spinner {{
    width: 44px;
    height: 44px;
    border: 4px solid rgba(0, 229, 155, 0.15);
    border-top: 4px solid #00e59b;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 20px;
}}
@keyframes spin {{
    0% {{ transform: rotate(0deg); }}
    100% {{ transform: rotate(360deg); }}
}}
h2 {{ margin: 0 0 8px 0; font-size: 1.25rem; font-weight: 600; }}
p {{ margin: 0; font-size: 0.95rem; color: #94a3b8; }}
a {{ color: #00e59b; text-decoration: none; font-weight: 500; }}
a:hover {{ text-decoration: underline; }}
.manual-link {{ margin-top: 20px; }}
</style>
</head>
<body>

<div class="spinner"></div>
<h2>Connessione a Script2...</h2>
<p>Reindirizzamento in corso...</p>

<div class="manual-link">
    <p>Se non vieni reindirizzato automaticamente: <a href="{link}">clicca qui per accedere a Script2</a></p>
</div>

</body>
</html>
"""


def update_github_repo_homepage(token: str, link: str) -> bool:
    """Aggiorna la homepage del repo GitHub tramite API (immediata, zero secondi di attesa)."""
    try:
        url = "https://api.github.com/repos/luishighnest/script2"
        data = json.dumps({"homepage": link}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Script2-Sync",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                print(f"[redirect] API GitHub Homepage aggiornata istantaneamente a: {link}")
                return True
    except Exception as e:
        print(f"[redirect] Attenzione: errore aggiornamento API GitHub homepage: {e}")
    return False


def main():
    if len(sys.argv) < 2:
        print("[redirect] Errore: manca il link come argomento")
        return 1
    link = sys.argv[1].strip()
    token = ""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        print("[redirect] Errore: github_token.txt non trovato")
        return 1

    # 1. Aggiorna subito l'API repo homepage (ha effetto in 0 secondi)
    update_github_repo_homepage(token, link)

    # 2. Aggiorna index.html per la copia statica di riserva
    INDEX.write_text(TEMPLATE.format(link=link), encoding="utf-8")

    repo_url = f"https://x-access-token:{token}@github.com/luishighnest/script2.git"
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.run(["git", "config", "user.name", "Render Auto-Sync"], cwd=str(BASE_DIR), check=True, creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "render-sync@users.noreply.github.com"], cwd=str(BASE_DIR), check=True, creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "add", "index.html", "update_redirect.py"], cwd=str(BASE_DIR), check=True, creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(BASE_DIR), creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "redirect: supporto dynamic real-time zero-cache redirect"], cwd=str(BASE_DIR), check=True, creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Effettua SEMPRE il push per garantire che le modifiche e l'index.html siano su GitHub
        subprocess.run(["git", "push", repo_url, "HEAD:main"], cwd=str(BASE_DIR), check=True, creationflags=no_win, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[redirect] File index.html e repo sincronizzati e pushati su GitHub.")
        return 0
    except Exception as e:
        print(f"[redirect] Errore push: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())