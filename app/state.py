"""Session admin partagée (en mémoire), réinitialisée à chaque changement de configuration."""
import threading

from . import config
from .zimbra import AdminSession, ZimbraError

_lock = threading.Lock()
_session: AdminSession | None = None


def set_admin_session(session: AdminSession | None) -> None:
    global _session
    with _lock:
        _session = session


def admin_session() -> AdminSession:
    """Session admin existante ou nouvelle (le jeton est obtenu/renouvelé à la demande)."""
    global _session
    s = config.get_settings()
    pwd = config.admin_password.get()
    if not s.admin_user or not pwd:
        raise ZimbraError("Compte admin ou mot de passe non saisi (écran Configuration)")
    with _lock:
        if _session is None:
            _session = AdminSession(s, pwd)
        return _session
