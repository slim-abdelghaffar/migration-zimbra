"""État réel des boîtes Zimbra (lecture seule) et resynchronisation de l'historique d'import.

La plateforme mémorise ce qu'elle a importé (table imported). Si la boîte est vidée ou modifiée dans Zimbra,
cet historique ne correspond plus à la réalité : resync() relit la boîte (Message-ID des mails, UID des
rendez-vous / tâches, nombre de contacts) et oublie les éléments qui n'y sont plus, pour qu'ils soient
réimportés au prochain lot — sans dupliquer ceux qui sont toujours présents.
"""
import json
import logging
import threading
import time

from . import config, db, state
from .zimbra import UserSession, ZimbraError, http_client

log = logging.getLogger("boites")

SESSION_TTL_S = 30 * 60
RELIABLE = 0.9  # part minimale de résultats porteurs d'un identifiant pour faire confiance à la recherche

_sessions: dict[str, tuple[UserSession, object, float]] = {}
_sessions_lock = threading.Lock()


def session(email: str) -> UserSession:
    """Session déléguée réutilisée quelques minutes (suivi en direct sans recréer un jeton à chaque appel)."""
    now = time.time()
    with _sessions_lock:
        cached = _sessions.get(email)
        if cached and now - cached[2] < SESSION_TTL_S:
            return cached[0]
        if cached:
            cached[1].close()
        client = http_client(config.get_settings().ca_file)
        user = UserSession(state.admin_session(), email, client)
        _sessions[email] = (user, client, now)
        return user


def drop_sessions() -> None:
    with _sessions_lock:
        for _, client, _ in _sessions.values():
            client.close()
        _sessions.clear()


# --- Taille de toutes les boîtes (admin) -------------------------------------------------

def refresh_usage() -> int:
    usage = state.admin_session().quota_usage(config.get_settings().domain)
    now = time.time()
    db.executemany("INSERT OR REPLACE INTO mailbox_usage(email, used, quota, at) VALUES(?,?,?,?)",
                   [(e, u["used"], u["limit"], now) for e, u in usage.items()])
    return len(usage)


_usage_lock = threading.Lock()
USAGE_MAX_AGE_S = 60


def refresh_usage_if_stale() -> None:
    """Rafraîchit la taille des boîtes en arrière-plan si elle date de plus d'une minute (un seul appel admin)."""
    r = db.query("SELECT MAX(at) AS t FROM mailbox_usage")
    if r and r[0]["t"] and time.time() - r[0]["t"] < USAGE_MAX_AGE_S:
        return
    if not _usage_lock.acquire(blocking=False):
        return

    def run() -> None:
        try:
            refresh_usage()
        except Exception as e:  # noqa: BLE001
            log.warning("Taille des boîtes indisponible : %s", e)
        finally:
            _usage_lock.release()

    threading.Thread(target=run, name="taille-boites", daemon=True).start()


def usage() -> dict[str, dict]:
    return {r["email"]: dict(r) for r in db.query("SELECT * FROM mailbox_usage")}


# --- État d'une boîte (délégué, lecture seule) ----------------------------------------------

VIEW_KIND = {"message": "mail", "appointment": "appt", "contact": "contact", "task": "task"}


def live_state(email: str) -> dict:
    """Dossiers et compteurs réels de la boîte, comparés à l'historique d'import de la plateforme."""
    folders = session(email).folder_tree()
    present = {k: 0 for k in VIEW_KIND.values()}
    for f in folders:
        k = VIEW_KIND.get(f["view"])
        if k:
            present[k] += f["n"]
    imported = {r["kind"]: r["n"] for r in db.query(
        "SELECT kind, COUNT(*) AS n FROM imported WHERE email = ? GROUP BY kind", (email,))}
    return {
        "email": email, "at": time.time(),
        "size": sum(f["size"] for f in folders),
        "unread": sum(f["unread"] for f in folders if f["view"] == "message"),
        "present": present,
        "imported": {k: imported.get(k, 0) for k in present},
        "folders": sorted((f for f in folders if f["n"] or f["view"] in VIEW_KIND), key=lambda f: f["path"].lower()),
    }


# --- Resynchronisation ------------------------------------------------------------------------

def resync(email: str, user: UserSession | None = None, logf=None) -> dict:
    """Oublie les éléments importés qui ne sont plus dans la boîte. Lecture seule côté Zimbra."""
    user = user or session(email)
    say = logf or (lambda m, level="INFO": log.info("%s : %s", email, m))
    rows = db.query("SELECT kind, folder, key FROM imported WHERE email = ?", (email,))
    by_kind: dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append((r["folder"], r["key"]))
    result = {"checked_at": time.time(), "kinds": {}, "removed": 0, "skipped": []}

    def forget(kind: str, keep) -> None:
        gone = [(f, k) for f, k in by_kind.get(kind, []) if not keep(f, k)]
        if gone:
            with db._lock, db.connect() as conn:
                conn.executemany("DELETE FROM imported WHERE email = ? AND kind = ? AND folder = ? AND key = ?",
                                 [(email, kind, f, k) for f, k in gone])
        result["kinds"][kind] = {"historique": len(by_kind.get(kind, [])), "absents": len(gone)}
        result["removed"] += len(gone)

    for kind, types, header in (("mail", "message", "Message-ID"), ("appt", "appointment", None), ("task", "task", None)):
        if not by_kind.get(kind):
            continue
        ids, hits, with_id = user.search_ids(types, header)
        if hits and with_id < RELIABLE * hits:
            # la recherche ne renvoie pas les identifiants : on ne touche à rien (pas de risque de doublons)
            result["skipped"].append(kind)
            say(f"Resynchronisation {kind} ignorée : identifiants illisibles ({with_id}/{hits})", "WARN")
            continue
        forget(kind, lambda f, k: k in ids)

    if by_kind.get("contact"):
        counts = {}
        for f in user.folder_tree():
            if f["view"] == "contact":
                counts[f["path"].strip("/")] = f["n"]
        # les contacts n'ont pas d'identifiant interrogeable : un carnet vidé est réimporté en entier
        forget("contact", lambda f, k: counts.get(f.strip("/"), 0) > 0)

    db.execute("INSERT OR REPLACE INTO mailbox_check(email, checked_at, removed, data) VALUES(?,?,?,?)",
               (email, result["checked_at"], result["removed"], json.dumps(result, ensure_ascii=False)))
    if result["removed"]:
        say(f"Boîte modifiée dans Zimbra : {result['removed']} élément(s) importé(s) n'y sont plus, ils seront réimportés")
    elif result["skipped"]:
        say("Vérification partielle : historique local conservé pour " + ", ".join(result["skipped"]), "WARN")
    else:
        say("Boîte conforme à l'historique d'import (aucun élément manquant)")
    return result


def last_check(email: str) -> dict | None:
    r = db.query("SELECT * FROM mailbox_check WHERE email = ?", (email,))
    return {**json.loads(r[0]["data"]), "removed": r[0]["removed"]} if r else None


def reset_history(email: str) -> int:
    """Efface l'historique d'import du compte : le prochain lot réimporte tout (risque de doublons si la boîte n'est pas vide)."""
    n = db.query("SELECT COUNT(*) AS n FROM imported WHERE email = ?", (email,))[0]["n"]
    db.execute("DELETE FROM imported WHERE email = ?", (email,))
    db.execute("INSERT OR REPLACE INTO mailbox_check(email, checked_at, removed, data) VALUES(?,?,?,?)",
               (email, time.time(), n, json.dumps({"reset": True, "removed": n})))
    log.info("Historique d'import effacé pour %s (%d éléments)", email, n)
    return n


# --- Vérification de toutes les boîtes migrées (tâche de fond) ---------------------------------

_job = {"running": False, "done": 0, "total": 0, "removed": 0, "error": None, "finished": None}
_job_lock = threading.Lock()


def job_state() -> dict:
    with _job_lock:
        return dict(_job)


def start_verify_all() -> bool:
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(running=True, done=0, total=0, removed=0, error=None)
    threading.Thread(target=_verify_all, name="verification-boites", daemon=True).start()
    return True


def _verify_all() -> None:
    try:
        refresh_usage()
        emails = [r["email"] for r in db.query("SELECT DISTINCT email FROM imported ORDER BY email")]
        with _job_lock:
            _job["total"] = len(emails)
        for e in emails:
            try:
                r = resync(e)
                with _job_lock:
                    _job["removed"] += r["removed"]
            except ZimbraError as ex:
                log.warning("Vérification de %s impossible : %s", e, ex)
            with _job_lock:
                _job["done"] += 1
    except Exception as ex:  # noqa: BLE001
        with _job_lock:
            _job["error"] = f"{type(ex).__name__} : {ex}"
    finally:
        with _job_lock:
            _job.update(running=False, finished=time.time())
