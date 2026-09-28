#!/usr/bin/env bash
# Script2 - riavvio su Termux: Flask + tunnel Cloudflare + aggiornamento redirect github.io
# (equivalente di "Riavvio Script2.bat" sul telefono)
# Uso:  bash $HOME/script2/termux/starter.sh
set -u

DIR="$HOME/script2"
CACHE="$HOME/.cache/script2"
TUNLOG="$CACHE/cf_tunnel.log"
FLLOG="$CACHE/flask_server.log"

mkdir -p "$CACHE"
cd "$DIR"

echo "============================================"
echo "  RIAVVIO SCRIPT2 (Termux)"
echo "============================================"

echo "[1/5] Arresto processi attivi (Flask + tunnel cloudflared)..."
pkill -f "python app.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
pkill -f "cloudflared-quick" 2>/dev/null || true
sleep 2

echo "[2/5] Avvio backend Flask..."
if [ "${SCRIPT2_NO_WARMUP:-1}" = "1" ]; then
  # Sul telefono il warmup del browser e' lento e quasi mai necessario
  # (la via veloce curl_cffi non usa il browser). Per attivarlo: export SCRIPT2_NO_WARMUP=0
  export SCRIPT2_NO_WARMUP=1
fi
PORT="${PORT:-5000}"
nohup python app.py >"$FLLOG" 2>&1 &
echo "  Flask avviato (pid $!, porte $PORT). Log: $FLLOG"

echo "[3/5] Avvio tunnel Cloudflare..."
rm -f "$TUNLOG"
cloudflared tunnel --edge-ip-version 4 --protocol http2 --url "http://127.0.0.1:$PORT" \
  --no-autoupdate --logfile "$TUNLOG" --loglevel info >/dev/null 2>&1 &

echo "[4/5] Attendo il link del tunnel..."
LINK=""
for i in $(seq 1 30); do
  sleep 2
  LINK=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNLOG" 2>/dev/null | tail -n1)
  [ -n "$LINK" ] && break
done

if [ -z "$LINK" ]; then
  echo "[!] Link del tunnel non trovato nel log."
  echo "    Il sito gira comunque in locale su http://127.0.0.1:$PORT"
  exit 1
fi
echo "  Tunnel corrente: $LINK"

echo "[5/5] Aggiorno il link ufficiale https://luishighnest.github.io/script2 al tunnel corrente..."
if [ -f "$DIR/github_token.txt" ]; then
  python "$DIR/update_redirect.py" "$LINK"
else
  echo "  [i] github_token.txt assente: salto l'aggiornamento."
  echo "  [i] Link da usare manualmente: $LINK"
fi

echo ""
echo "FATTO! Sito ufficiale: https://luishighnest.github.io/script2"
echo "Tenere il telefono acceso e in carica; la luce del tunnel si spegne se l'app Termux viene chiusa."
echo "Nota ASN: i token estratti prenderanno l'ASN di questa connessione (WARP/casa), non 13335."