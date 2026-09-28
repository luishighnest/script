#!/usr/bin/env bash
# Script2 - installazione one-shot su Termux (Android)
# Uso:  bash setup_termux.sh
set -e

echo "============================================"
echo "  SCRIPT2 - SETUP TERMUX (installazione)"
echo "============================================"

echo "[1/6] Aggiornamento pacchetti di sistema..."
pkg update -y && pkg upgrade -y

echo "[2/6] Installazione pacchetti di sistema..."
pkg install -y python cloudflared git curl coreutils

echo "[3/6] Clonazione/aggiornamento del repo script2..."
if [ ! -d "$HOME/script2/.git" ]; then
  git clone https://github.com/luishighnest/script2.git "$HOME/script2"
else
  git -C "$HOME/script2" pull --rebase
fi

echo "[4/6] Dipendenze Python (pacchetto per pacchetto, senza bloccare)..."
# Su Termux alcune librerie non hanno wheel (playwright, a volte curl_cffi/pydantic):
# si prova a installare tutto, si segnano i falliti e si va avanti.

skip_pkg() {
  case "$1" in
    playwright*|gunicorn*) return 0 ;;
    curl_cffi*) echo "  [speciale] $1  -> si compila da sorgente (build_curl_cffi.sh)"; return 0 ;;
    *) return 1 ;;
  esac
}

cd "$HOME/script2"
FAILED=""
while read -r pkg; do
  pkg="${pkg%%#*}"; pkg="${pkg// /}"
  [ -z "$pkg" ] && continue
  if skip_pkg "$pkg"; then
    echo "  [saltato] $pkg  (non serve o senza wheel su Android)"
    continue
  fi
  echo "  [installo] $pkg ..."
  if timeout 420 pip install "$pkg" >/dev/null 2>&1; then
    echo "  [ok] $pkg"
  else
    echo "  [FALLITO] $pkg"
    FAILED="$FAILED
  - $pkg"
  fi
done < requirements.txt

if [ -n "$FAILED" ]; then
  echo ""
  echo "  [i] Alcuni pacchetti non sono stati installati:$FAILED"
  echo "      curl_cffi (estensione Rust) NON si installa con pip su Android:"
  echo "      va compilato con libcurl-impersonate (30-60 min, una sola volta):"
  echo "        pkg install -y rust clang binutils make pkg-config libcurl maturin"
  echo "        bash \$HOME/script2/termux/build_curl_cffi.sh"
  echo "      Altri pacchetti Rust (pydantic, pywidevine):"
  echo "        pkg install -y rust clang binutils make pkg-config libcurl"
  echo "        pip install pydantic pydantic-settings pywidevine"
fi

echo "[5/6] Browser di fallback per l'estrazione (OPZIONALE)..."
# La via veloce (curl_cffi) non usa il browser e basta per la maggior parte
# delle estrazioni. Chrome/Chromium headless serve solo per le licenze che
# pretendono i cookie del profilo dentro un browser reale.
if python -c "import playwright" 2>/dev/null; then
  python -m playwright install chromium || echo "  [!] Playwright chromium non installato: si usera' solo la via veloce."
fi

echo "[6/6] Token GitHub (OPZIONALE, per l'aggiornamento automatico del redirect)..."
if [ ! -f "$HOME/script2/github_token.txt" ]; then
  echo "  Niente github_token.txt trovato."
  echo "  Se vuoi che https://luishighnest.github.io/script2 punti al tunnel del telefono:"
  echo "    cp /sdcard/Download/github_token.txt \$HOME/script2/github_token.txt"
  echo "  (o crealo con: nano \$HOME/script2/github_token.txt  e incolla il token)"
fi

echo "============================================"
echo "  FATTO. Ora avvia con:  bash \$HOME/script2/termux/starter.sh"
echo "============================================"