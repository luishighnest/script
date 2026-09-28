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
pkg install -y python cloudflared git curl

echo "[3/6] Clonazione/aggiornamento del repo script2..."
if [ ! -d "$HOME/script2/.git" ]; then
  git clone https://github.com/luishighnest/script2.git "$HOME/script2"
else
  git -C "$HOME/script2" pull --rebase
fi

echo "[4/6] Dipendenze Python (Flask, Playwright, curl_cffi, pywidevine...)..."
pip install --upgrade pip
pip install -r "$HOME/script2/requirements.txt"

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