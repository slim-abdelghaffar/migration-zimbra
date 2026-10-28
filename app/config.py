"""Configuration : paramètres non secrets en SQLite, mot de passe admin en mémoire uniquement."""
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import db

DEFAULTS = {
    "admin_url": "https://mailbox-01.courdescomptes.nat.tn:7071/service/admin/soap",
    "user_url": "https://mailbox.courdescomptes.nat.tn",
    "admin_user": "",
    "pst_dir": str(Path.home() / "migration-zimbra" / "pst"),
    "parallelism": "3",
    "ca_file": "",
    "timezone": "Africa/Tunis",
    "domain": "",
    "connections": "4",
}
MAX_CONNECTIONS = 8
MAX_PARALLELISM = 5


@dataclass(frozen=True)
class Settings:
    admin_url: str
    user_url: str
    admin_user: str
    pst_dir: str
    parallelism: int
    ca_file: str  # bundle CA supplémentaire (PEM), vide = magasin système
    timezone: str  # fuseau par défaut des rendez-vous / dates sans fuseau dans le PST
    domain: str    # domaine Zimbra à inventorier (vide = tous)
    connections: int  # envois simultanés par compte

    @property
    def admin_host(self) -> str:
        return urlsplit(self.admin_url).hostname or ""

    @property
    def admin_port(self) -> int:
        return urlsplit(self.admin_url).port or 443

    @property
    def user_host(self) -> str:
        return urlsplit(self.user_url).hostname or ""

    @property
    def user_port(self) -> int:
        return urlsplit(self.user_url).port or 443

    @property
    def user_soap_url(self) -> str:
        return self.user_url.rstrip("/") + "/service/soap"


class _Secret:
    """Mot de passe admin : en mémoire du processus, jamais écrit ni journalisé."""

    def __init__(self):
        self._value: str | None = None
        self._lock = threading.Lock()

    def set(self, value: str | None) -> None:
        with self._lock:
            self._value = value or None

    def get(self) -> str | None:
        with self._lock:
            return self._value

    def __repr__(self) -> str:  # évite toute fuite via repr()/logs
        return "<secret défini>" if self._value else "<secret vide>"


admin_password = _Secret()


def get_settings() -> Settings:
    raw = {**DEFAULTS, **db.load_settings()}
    return Settings(
        admin_url=raw["admin_url"],
        user_url=raw["user_url"],
        admin_user=raw["admin_user"],
        pst_dir=raw["pst_dir"],
        parallelism=int(raw["parallelism"]),
        ca_file=raw["ca_file"],
        timezone=raw["timezone"],
        domain=raw["domain"],
        connections=int(raw["connections"]),
    )


def validate(form: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Retourne (valeurs nettoyées, erreurs)."""
    errors = []
    v = {k: (form.get(k) or "").strip() for k in DEFAULTS}

    for key, label in (("admin_url", "URL admin"), ("user_url", "URL utilisateur")):
        parts = urlsplit(v[key])
        if parts.scheme != "https" or not parts.hostname:
            errors.append(f"{label} : doit commencer par https:// et contenir un nom d'hôte.")
    v["user_url"] = v["user_url"].rstrip("/")

    if v["admin_user"] and "@" not in v["admin_user"]:
        errors.append("Compte admin : adresse complète attendue (ex. admin@domaine).")

    try:
        p = int(v["parallelism"])
        if not 1 <= p <= MAX_PARALLELISM:
            raise ValueError
    except ValueError:
        errors.append(f"Parallélisme : entier entre 1 et {MAX_PARALLELISM}.")

    try:
        c = int(v["connections"] or 4)
        if not 1 <= c <= MAX_CONNECTIONS:
            raise ValueError
        v["connections"] = str(c)
    except ValueError:
        errors.append(f"Envois simultanés par compte : entier entre 1 et {MAX_CONNECTIONS}.")

    if v["ca_file"] and not Path(v["ca_file"]).is_file():
        errors.append(f"Fichier CA introuvable : {v['ca_file']}")

    v["domain"] = v["domain"].lower().lstrip("@")
    if v["domain"] and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", v["domain"]):
        errors.append(f"Domaine invalide : {v['domain']}")

    try:
        ZoneInfo(v["timezone"])
    except Exception:  # noqa: BLE001
        errors.append(f"Fuseau horaire inconnu : {v['timezone']} (ex. Africa/Tunis)")

    return v, errors


def save(values: dict[str, str]) -> None:
    db.save_settings({k: values[k] for k in DEFAULTS})


def as_dict(s: Settings) -> dict:
    return asdict(s)
