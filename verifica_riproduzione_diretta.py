"""
Verifica end-to-end della RIPRODUZIONE DIRETTA (nessun proxy).

Simula esattamente cio' che fa Kodi + inputstream.adaptive:
  1. scarica il manifest dall'URL CDN che contiene gia' il token
  2. risolve i BaseURL (DAZN usa BaseURL relativi: "../all/", "dash/")
  3. scarica gli init segment e i media segment
  4. invia SOLO l'header User-Agent, come fa manifest_headers/stream_headers

Se questo test passa, l'addon puo' riprodurre direttamente: nessun proxy,
nessun worker, nessun Cloudflare necessario.
"""
import json
import sys
import urllib.parse
from xml.etree import ElementTree as ET

from curl_cffi import requests as creq

D = "{urn:mpeg:dash:schema:mpd:2011}"
DOLLAR = chr(36)


def _fill(template, rep_id, time_value="0"):
    return (
        template.replace(DOLLAR + "RepresentationID" + DOLLAR, rep_id or "0")
        .replace(DOLLAR + "Time" + DOLLAR, time_value)
        .replace(DOLLAR + "Number" + DOLLAR, "1")
        .replace(DOLLAR + "Bandwidth" + DOLLAR, "0")
        .replace(DOLLAR + "SubNumber" + DOLLAR, "0")
    )


def _resolve_base_urls(root, manifest_url):
    base = manifest_url
    for b in root.iter(D + "BaseURL"):
        base = urllib.parse.urljoin(base, (b.text or "").strip())
    return base


def _segment_urls(root, base):
    urls = []
    for aset in root.iter(D + "AdaptationSet"):
        st = aset.find(D + "SegmentTemplate")
        reps = aset.findall(D + "Representation")
        if st is None or not reps:
            continue
        rep_id = reps[0].get("id")
        ctype = aset.get("contentType")
        if st.get("initialization"):
            urls.append((ctype, "init", urllib.parse.urljoin(base, _fill(st.get("initialization"), rep_id))))
        media = st.get("media")
        timeline = st.find(D + "SegmentTimeline")
        if media and timeline is not None and timeline.find(D + "S") is not None:
            t0 = timeline.find(D + "S").get("t") or "0"
            urls.append((ctype, "media", urllib.parse.urljoin(base, _fill(media, rep_id, t0))))
    return urls


def verifica(mpd_url, user_agent, etichetta=""):
    print("=" * 74)
    print("VERIFICA RIPRODUZIONE DIRETTA %s" % (etichetta or ""))
    print("=" * 74)
    print("URL manifest : %s" % mpd_url[:88])
    print("User-Agent   : %s" % user_agent)
    print()

    # Kodi invia l'UA del manifest anche per i segmenti (stream_headers).
    headers = {"User-Agent": user_agent}
    session = creq.Session()

    r = session.get(mpd_url, headers=headers, timeout=20)
    print("[1] Manifest DASH  -> HTTP %s (%s byte)" % (r.status_code, len(r.content)))
    if r.status_code != 200:
        print("    ERRORE: la CDN ha rifiutato il manifest.")
        return False

    root = ET.fromstring(r.content)
    base = _resolve_base_urls(root, r.url)
    print("[2] BaseURL risolto -> %s" % base)
    if "indazn.com/@" not in base and "dazn" not in base:
        print("    ERRORE: il BaseURL non punta alla CDN.")
        return False

    urls = _segment_urls(root, base)
    if not urls:
        print("[3] Nessun segmento nel manifest.")
        return False

    ok = 0
    for ctype, kind, url in urls:
        r2 = session.get(url, headers={**headers, "Range": "bytes=0-8191"}, timeout=20)
        good = r2.status_code in (200, 206)
        ok += good
        print("    %-5s %-5s HTTP %-3s %6s byte  %s" % (
            ctype, kind, r2.status_code, len(r2.content), "OK" if good else "FALLITO"))

    print()
    print("RISULTATO: %d/%d segmenti scaricati DIRETTAMENTE dalla CDN (solo User-Agent, ZERO proxy)"
          % (ok, len(urls)))
    return ok == len(urls)


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as fh:
            eventi = json.load(fh)
        target = None
        for items in eventi.values():
            for ev in items:
                if ev.get("mpd") and ev.get("ua"):
                    target = ev
                    break
            if target:
                break
        if not target:
            print("Nessun evento con 'mpd' e 'ua' trovato.")
            return 1
        return 0 if verifica(target["mpd"], target["ua"], "- %s" % target.get("name")) else 1

    print("Uso: python verifica_riproduzione_diretta.py [dazn_event.json]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
