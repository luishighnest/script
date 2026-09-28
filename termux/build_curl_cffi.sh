#!/usr/bin/env bash
# Compila curl_cffi (con libcurl-impersonate) per Termux/Android.
# Necessario per l'estrazione "identica al PC" (via veloce con TLS-fingerprint).
# Tempo stimato: 30-60 minuti. Uso:  bash build_curl_cffi.sh
set -e

echo "============================================"
echo "  BUILD curl_cffi + libcurl-impersonate"
echo "  (Termux/Android - lunga, ~30-60 min)"
echo "============================================"

echo "[1/6] Toolchain di sistema..."
pkg reinstall -y openssl openssl-static || true
pkg install -y build-essential pkg-config cmake ninja curl autoconf automake libtool golang jq binutils rust git termux-elf-cleaner

echo "[2/6] maturin (compilatore wheel)..."
export CARGO_BUILD_TARGET="$(rustc -Vv | grep '^host' | awk '{print $2}')"
echo "  CARGO_BUILD_TARGET=$CARGO_BUILD_TARGET"
retry=1
while ! command -v maturin >/dev/null 2>&1 && (( retry < 10 )); do
  pip install -U maturin || true
  (( retry++ ))
  sleep 2
done
if ! command -v maturin >/dev/null 2>&1; then
  echo "[!] maturin non installato. Riprova e copiami l'errore."
  exit 1
fi
echo "  maturin ok: $(command -v maturin)"

echo "[3/6] Sorgenti curl_cffi..."
cd "$HOME"
rm -rf curl_cffi
git clone --depth=1 https://github.com/yifeikong/curl_cffi
cd curl_cffi

echo "[4/6] Patch per build su Android..."
sed -i '/delocate-wheel/d' Makefile 2>/dev/null || true
sed -i 's/delocate//g' Makefile 2>/dev/null || true
sed -i '/urlretrieve(/d' scripts/build.py 2>/dev/null || true

echo "[5/6] Compilazione curl-impersonate + estensione Python (il passo lungo)..."
make build

echo "[6/6] Installazione wheel..."
pip uninstall -y curl_cffi || true
pip install --force-reinstall dist/curl_cffi-*.whl

echo "  Librerie impersonate (se presenti):"
ls -1 "$PREFIX/lib"/libcurl-impersonate* 2>/dev/null || echo "  (nessuna lib in \$PREFIX/lib)"

echo "Verifica finale (fingerprint TLS)..."
python - <<'PY'
try:
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome131")
    r = s.get("https://tls.peet.ws/api/all", timeout=20)
    data = r.json()
    ja3 = (data.get("tls") or {}).get("ja3", "?")
    print("OK curl_cffi | JA3:", ja3)
    print("server:", r.headers.get("server", "?"))
except Exception as e:
    print("FALLITO:", e)
    raise SystemExit(1)
PY

echo ""
echo "FATTO. curl_cffi installato e verificato."
echo "Ora avvia Script2:  bash \$HOME/script2/termux/starter.sh"