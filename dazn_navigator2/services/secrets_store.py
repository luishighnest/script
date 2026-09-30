"""Lettura dei segreti locali (Upstash).

Il token Upstash era scritto dentro il codice, in piu' file. Essendo il
repository pubblico su GitHub, chiunque poteva leggere e scrivere il
database. Ora il valore vive in `.secrets.json`, nella root del progetto,
coperto da `.gitignore`.

Questo modulo esiste perche' sia `app.py` sia `eventi_cmds.py` hanno bisogno
del token: importare `app` da `eventi_cmds` creerebbe un ciclo, quindi la
lettura sta qui, senza dipendenze.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
SECRETS_FILE = BASE_DIR / ".secrets.json"


def upstash_token():
    """Ritorna il token Upstash, oppure "" se non configurato.

    L'ordine di preferenza e': variabile d'ambiente, poi file locale.
    Se non c'e' nessuna delle due si restituisce stringa vuota: le chiamate
    verso Upstash falliranno, ma il resto del programma continua a
    funzionare come prima.
    """
    env = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if env:
        return env
    try:
        if SECRETS_FILE.exists():
            return json.loads(SECRETS_FILE.read_text(encoding="utf-8")).get(
                "upstash_token", ""
            )
    except Exception:
        pass
    return ""
