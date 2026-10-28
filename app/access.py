"""Accès à l'interface : mot de passe d'accès (empreinte scrypt sur disque) et sessions en mémoire.

Le mot de passe d'accès protège l'interface quand elle est ouverte sur le réseau. Il est distinct
du mot de passe admin Zimbra (qui, lui, n'est jamais écrit sur disque).
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .db import STATE_DIR

ACCESS_FILE = STATE_DIR / "data" / "access.json"
SESSION_TTL_S = 12 * 3600
COOKIE = "ns_session"

_sessions: dict[str, float] = {}
_lock = threading.Lock()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 << 20).hex()


def set_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("au moins 10 caractères")
    salt = secrets.token_bytes(16)
    ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(password, salt)}))
    os.chmod(tmp, 0o600)
    tmp.replace(ACCESS_FILE)
    with _lock:
        _sessions.clear()


def enabled() -> bool:
    return ACCESS_FILE.is_file()


def check_password(password: str) -> bool:
    try:
        data = json.loads(ACCESS_FILE.read_text())
    except (OSError, ValueError):
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(data["salt"])), data["hash"])


def open_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL_S
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if exp is None or exp < time.time():
            _sessions.pop(token, None)
            return False
        return True


def close_session(token: str | None) -> None:
    with _lock:
        _sessions.pop(token or "", None)


if __name__ == "__main__":  # python -m app.access : définir le mot de passe d'accès
    import getpass
    import sys
    p1 = getpass.getpass("Nouveau mot de passe d'accès à l'interface (10 caractères min.) : ")
    p2 = getpass.getpass("Confirmer : ")
    if p1 != p2:
        sys.exit("Les deux saisies diffèrent.")
    try:
        set_password(p1)
    except ValueError as e:
        sys.exit(f"Mot de passe refusé : {e}")
    print(f"Mot de passe d'accès enregistré (empreinte dans {ACCESS_FILE}).")
