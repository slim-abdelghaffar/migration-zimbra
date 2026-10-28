"""Inventaire : annuaire Zimbra + fichiers PST + attribution automatique de chaque PST à son propriétaire.

Attribution, par ordre de fiabilité :
  1. une adresse email exacte dans le nom du fichier ou d'un dossier parent -> validée automatiquement ;
  2. le nom du fichier / d'un dossier correspond à un compte (prenom.nom, « NOM Prénom »…) -> proposée ;
  3. le contenu du PST (expéditeur des Éléments envoyés, destinataire de la Boîte de réception) -> proposée.
Une proposition doit être validée dans l'interface avant toute migration.
"""
import json
import logging
import re
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher, get_close_matches
from itertools import permutations
from pathlib import Path

from . import config, db, pst, state

log = logging.getLogger("inventaire")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

READY = "prêt"
TO_VALIDATE = "à valider"
UNASSIGNED = "non attribué"
NOT_FOUND = "compte introuvable"
UNCHECKED = "non vérifié"
INVALID = "fichier invalide"
COPYING = "copie en cours"
COPY_WAIT_S = 3600  # attente maximale de la fin d'une copie
AUTO = 100

# --- Tâche de fond (annuaire + analyse des PST) ----------------------------------------

_job = {"running": False, "step": "", "done": 0, "total": 0, "error": None, "finished": None}
_job_lock = threading.Lock()


def job_state() -> dict:
    with _job_lock:
        return dict(_job)


def _set(**kw) -> None:
    with _job_lock:
        _job.update(kw)


def start_job() -> bool:
    """Lance synchronisation de l'annuaire + analyse des PST en arrière-plan (False si déjà en cours)."""
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(running=True, step="annuaire", done=0, total=0, error=None)
    threading.Thread(target=_run_job, name="inventaire", daemon=True).start()
    return True


def _run_job() -> None:
    try:
        n = sync_directory()
        log.info("Annuaire synchronisé : %d comptes", n)
        try:
            from . import mailbox
            mailbox.refresh_usage()
        except Exception as e:  # noqa: BLE001 — la taille des boîtes est un plus, pas un prérequis
            log.warning("Taille des boîtes indisponible : %s", e)
        _set(step="pst")
        rows = scan(lambda d, t: _set(done=d, total=t))
        log.info("Inventaire : %d PST, %d prêts", len(rows), sum(r["status"] == READY for r in rows))
        _wait_copies([Path(r["file"]) for r in rows if r["status"] == COPYING])
    except Exception as e:  # noqa: BLE001
        log.warning("Inventaire en échec : %s", e)
        _set(error=f"{type(e).__name__} : {e}")
    finally:
        _set(running=False, step="", finished=time.time())


def _wait_copies(files: list[Path]) -> None:
    """Attend la fin des copies en cours puis analyse ces fichiers automatiquement."""
    deadline = time.time() + COPY_WAIT_S
    while files and time.time() < deadline:
        _set(step="copie", done=0, total=len(files))
        time.sleep(5)
        done = [f for f in files if f.is_file() and not pst.is_copying(f)]
        if not done:
            continue
        root = Path(config.get_settings().pst_dir)
        dir_ = directory()
        manual = {r["file"]: r["email"] for r in db.query("SELECT file, email FROM pst_assoc")}
        _save([analyse_file(f, root, dir_, name_index(dir_), manual) for f in done], replace=False)
        log.info("Copie terminée, PST analysé(s) : %s", ", ".join(f.name for f in done))
        files = [f for f in files if f not in done and f.is_file()]


# --- Annuaire Zimbra --------------------------------------------------------------------

def sync_directory() -> int:
    s = config.get_settings()
    accounts = state.admin_session().list_accounts(s.domain)
    now = time.time()
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM directory")
        conn.executemany("INSERT INTO directory(email, id, login, display_name, status, synced_at) VALUES(?,?,?,?,?,?)",
                         [(a["email"], a["id"], a.get("login", ""), a["display_name"], a["status"], now) for a in accounts])
    return len(accounts)


def directory() -> dict[str, dict]:
    return {r["email"]: dict(r) for r in db.query("SELECT * FROM directory ORDER BY email")}


def directory_synced_at() -> float | None:
    r = db.query("SELECT MAX(synced_at) AS t FROM directory")
    return r[0]["t"] if r else None


# --- Correspondance de noms -------------------------------------------------------------

# Mots fréquents dans les noms de PST qui ne désignent pas une personne
NOISE = {"archive", "archives", "archivage", "outlook", "pst", "backup", "sauvegarde", "save", "old", "ancien",
         "anciens", "ancienne", "mail", "mails", "email", "messagerie", "boite", "fichier", "fichiers", "donnees",
         "data", "file", "files", "export", "copie", "copy", "perso", "personnel", "personal", "folders", "dossiers",
         "documents", "users", "utilisateurs", "bureau", "desktop", "de", "du", "la", "le", "les", "des", "et",
         "partie", "superieure", "top", "of", "pc", "laptop", "new", "nouveau", "v", "bis"}
GENERIC_STORES = {"outlook data file", "fichier de donnees outlook", "personal folders", "dossiers personnels",
                  "boite aux lettres", "mailbox", "archive"}
FUZZY_CUTOFF = 0.88


def _tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z0-9]+", s)


def _clean(label: str) -> list[str]:
    """Mots significatifs d'un nom de fichier / dossier (sans années, numéros ni mots génériques)."""
    return [t for t in _tokens(label) if t not in NOISE and not t.isdigit()]


def _joins(tokens: list[str]) -> set[str]:
    """Toutes les façons d'écrire ces mots collés, dans tous les ordres (« ben abdelghaffar slim » =
    « slimbenabdelghaffar ») ; limité à 4 mots."""
    if not tokens:
        return set()
    if len(tokens) > 4:
        return {"".join(tokens), "".join(sorted(tokens))}
    return {"".join(p) for p in permutations(tokens)}


class NameMatcher:
    """Rapproche un nom de fichier / dossier d'un compte de l'annuaire.

    Niveaux (fiabilité) : nom complet dans n'importe quel ordre (85), identifiant Windows/AD (85),
    initiale + nom (70), nom approchant / faute de frappe (65), nom de famille seul s'il est unique (60).
    """

    def __init__(self, dir_: dict):
        self.full: dict[str, set[str]] = {}
        self.login: dict[str, set[str]] = {}
        self.initials: dict[str, set[str]] = {}
        self.last: dict[str, set[str]] = {}
        for email, acc in dir_.items():
            local = _tokens(email.split("@")[0])
            for toks in (local, _clean(acc.get("display_name") or "")):
                if len(toks) < 1 or len("".join(toks)) < 4:
                    continue
                for j in _joins(toks):
                    self.full.setdefault(j, set()).add(email)
                if len(toks) >= 2:
                    # convention prenom.nom et nom.prenom : initiale + nom, nom seul
                    for first, last in ((toks[0], "".join(toks[1:])), (toks[-1], "".join(toks[:-1]))):
                        for k in (first[0] + last, last + first[0]):
                            self.initials.setdefault(k, set()).add(email)
                        if len(last) >= 5:
                            self.last.setdefault(last, set()).add(email)
            login = "".join(_tokens(acc.get("login") or ""))
            if len(login) >= 4:
                self.login.setdefault(login, set()).add(email)
        self._full_keys = list(self.full)

    def match(self, label: str) -> list[tuple[str, int, str]]:
        toks = _clean(label)
        if not toks or len("".join(toks)) < 4:
            return []
        joins = _joins(toks)
        for table, conf, how in ((self.full, 85, "nom complet"), (self.login, 85, "identifiant Windows/AD"),
                                 (self.initials, 70, "initiale + nom"), (self.last, 60, "nom de famille")):
            found = set().union(*(table.get(j, set()) for j in joins))
            if found:
                return [(e, conf if len(found) == 1 else 45, how + ("" if len(found) == 1 else " (ambigu)"))
                        for e in found]
        # faute de frappe : un seul compte nettement proche
        best: dict[str, float] = {}
        for j in joins:
            for k in get_close_matches(j, self._full_keys, n=3, cutoff=FUZZY_CUTOFF):
                for e in self.full[k]:
                    best[e] = max(best.get(e, 0), SequenceMatcher(None, j, k).ratio())
        if len(best) == 1:
            e, r = next(iter(best.items()))
            return [(e, 65, f"nom approchant ({round(100 * r)} % de similarité)")]
        return [(e, 40, "nom approchant (ambigu)") for e in best]


def name_index(dir_: dict) -> NameMatcher:
    return NameMatcher(dir_)


# --- Identification par le contenu (java-libpst) ------------------------------------------

def identify(f: Path) -> dict:
    """Expéditeurs des envoyés / destinataires reçus (résultat mis en cache par taille + date)."""
    st = f.stat()
    rows = db.query("SELECT * FROM pst_ident_cache WHERE file = ?", (str(f),))
    if rows and rows[0]["size"] == st.st_size and rows[0]["mtime"] == st.st_mtime:
        return json.loads(rows[0]["result"])
    try:
        out = subprocess.run(["java", "-Xmx384m", "-cp", f"{pst.JAR}:{pst.CLASSES}", "com.pff.PstIdentify", str(f)],
                             capture_output=True, text=True, timeout=600)
        result = json.loads(out.stdout) if out.returncode == 0 else {"error": out.stderr.strip()[-300:]}
    except (subprocess.TimeoutExpired, ValueError) as e:
        result = {"error": f"{type(e).__name__} : {e}"}
    db.execute("INSERT OR REPLACE INTO pst_ident_cache(file, size, mtime, result) VALUES(?,?,?,?)",
               (str(f), st.st_size, st.st_mtime, json.dumps(result)))
    return result


# --- Analyse d'un fichier -------------------------------------------------------------------

def analyse_file(f: Path, root: Path, dir_: dict, idx: dict, manual: dict) -> dict:
    rel = f.relative_to(root)
    problem = pst.check_pst_file(f)
    if problem:
        stem = f.name[:-4].strip().lower()
        copying = problem == COPYING
        return {"file": str(f), "size": f.stat().st_size, "email": stem if EMAIL_RE.match(stem) else None,
                "method": "", "confidence": 0, "status": COPYING if copying else INVALID,
                "detail": "analyse automatique dès la fin de la copie" if copying else problem,
                "candidates": [], "content": {}}
    comps = [(f.name[:-4], "nom du fichier")] + [(p, f"dossier « {p} »") for p in reversed(rel.parts[:-1])]
    cands: dict[str, dict] = {}
    notes = []

    def add(email: str, conf: int, method: str) -> None:
        c = cands.setdefault(email, {"email": email, "confidence": 0, "methods": []})
        c["confidence"] = max(c["confidence"], conf)
        if method not in c["methods"]:
            c["methods"].append(method)

    unknown_email = None
    for label, where in comps:
        if EMAIL_RE.match(label.strip()):
            e = label.strip().lower()
            if e in dir_:
                add(e, AUTO, f"adresse exacte ({where})")
            else:
                unknown_email = unknown_email or e
            continue
        for e, conf, how in idx.match(label):
            add(e, conf, f"{how} ({where})")

    content = identify(f) if pst.extractor_ready() is None else {"error": "extracteur indisponible"}
    store = (content.get("store") or "").strip()
    if store and " ".join(_tokens(store)) not in GENERIC_STORES:
        for e, conf, how in idx.match(store):  # nom interne du PST (souvent le nom ou l'adresse du titulaire)
            add(e, conf, f"{how} (nom interne du PST « {store} »)")
        if EMAIL_RE.match(store) and store.lower() in dir_:
            add(store.lower(), 85, "adresse dans le nom interne du PST")
    content_top = None
    for key, label, strong in (("sent", "expéditeur des Éléments envoyés", 90), ("received", "destinataire des mails reçus", 85)):
        lst = content.get(key) or []
        total = sum(x["n"] for x in lst)
        if lst and total:
            top = lst[0]
            content_top = content_top or top["addr"]
            if top["addr"] in dir_:
                share = top["n"] / total
                add(top["addr"], strong if share >= 0.5 else strong - 25,
                    f"contenu : {label} ({top['n']}/{total})")

    for c in cands.values():
        by_name = any(not m.startswith("contenu") for m in c["methods"])
        by_content = any(m.startswith("contenu") for m in c["methods"])
        if by_name and by_content and c["confidence"] >= 60 and c["confidence"] < AUTO:
            # deux preuves indépendantes (nom du fichier / dossier ET contenu) : attribution automatique
            c["confidence"] = AUTO
            c["methods"].append("nom et contenu concordants")
        elif len(c["methods"]) >= 2 and c["confidence"] < AUTO:
            c["confidence"] = min(99, c["confidence"] + 10)

    row = {"file": str(f), "size": f.stat().st_size, "email": None, "method": "", "confidence": 0,
           "status": UNASSIGNED, "detail": "", "candidates": sorted(cands.values(), key=lambda c: -c["confidence"]),
           "content": content}
    best = row["candidates"][0] if row["candidates"] else None

    if str(f) in manual and manual[str(f)] in dir_:
        row.update(email=manual[str(f)], method="validé manuellement", confidence=AUTO, status=READY)
    elif not dir_:
        row.update(status=UNCHECKED, detail="annuaire Zimbra non synchronisé (mot de passe admin requis)")
        if best:
            row.update(email=best["email"], method=", ".join(best["methods"]), confidence=best["confidence"])
    elif best and best["confidence"] >= AUTO:
        row.update(email=best["email"], method=", ".join(best["methods"]), confidence=AUTO, status=READY)
    elif best:
        row.update(email=best["email"], method=", ".join(best["methods"]), confidence=best["confidence"],
                   status=TO_VALIDATE)
        if len(row["candidates"]) > 1:
            notes.append("plusieurs propriétaires possibles : " + ", ".join(c["email"] for c in row["candidates"]))
    elif unknown_email:
        row.update(email=unknown_email, status=NOT_FOUND, detail=f"{unknown_email} n'existe pas dans Zimbra")

    if row["email"] and content_top and content_top != row["email"] and row["status"] in (READY, TO_VALIDATE):
        notes.append(f"attention : le contenu appartient plutôt à {content_top}")
    elif row["status"] == UNASSIGNED and content_top:
        notes.append(f"contenu : {content_top} (absent de l'annuaire)")
    if content.get("error"):
        notes.append("lecture du contenu impossible")
    if notes:
        row["detail"] = "; ".join(filter(None, [row["detail"], *notes]))
    return row


def _save(rows: list[dict], replace: bool) -> None:
    now = time.time()
    with db._lock, db.connect() as conn:
        if replace:
            conn.execute("DELETE FROM pst_files")
        conn.executemany(
            """INSERT OR REPLACE INTO pst_files(file, size, email, method, confidence, status, detail, candidates,
               content, checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [(r["file"], r["size"], r["email"], r["method"], r["confidence"], r["status"], r["detail"],
              json.dumps(r["candidates"], ensure_ascii=False), json.dumps(r["content"], ensure_ascii=False), now)
             for r in rows])


def scan(progress=lambda done, total: None) -> list[dict]:
    """Analyse récursive du dossier PST et attribution des propriétaires."""
    s = config.get_settings()
    root = Path(s.pst_dir)
    if not root.is_dir():
        raise RuntimeError(f"Dossier PST introuvable : {root}")
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pst")
    dir_ = directory()
    idx = name_index(dir_)
    manual = {r["file"]: r["email"] for r in db.query("SELECT file, email FROM pst_assoc")}
    rows, done = [], 0
    progress(0, len(files))
    with ThreadPoolExecutor(max_workers=s.parallelism) as ex:
        for row in ex.map(lambda f: analyse_file(f, root, dir_, idx, manual), files):
            rows.append(row)
            done += 1
            progress(done, len(files))
    _save(rows, replace=True)
    return rows


# --- Validation ---------------------------------------------------------------------------

def validate(file: str, email: str) -> None:
    email = email.strip().lower()
    if email not in directory():
        raise RuntimeError(f"{email} n'existe pas dans l'annuaire Zimbra")
    if not db.query("SELECT 1 FROM pst_files WHERE file = ?", (file,)):
        raise RuntimeError("Fichier PST inconnu de l'inventaire")
    db.execute("INSERT OR REPLACE INTO pst_assoc(file, email, validated_at) VALUES(?,?,?)", (file, email, time.time()))
    db.execute("UPDATE pst_files SET email = ?, status = ?, method = 'validé manuellement', confidence = ? WHERE file = ?",
               (email, READY, AUTO, file))
    log.info("Attribution validée : %s -> %s", Path(file).name, email)


def validate_all_proposed() -> int:
    rows = db.query("SELECT file, email FROM pst_files WHERE status = ? AND email IS NOT NULL", (TO_VALIDATE,))
    for r in rows:
        validate(r["file"], r["email"])
    return len(rows)


def unvalidate(file: str) -> None:
    db.execute("DELETE FROM pst_assoc WHERE file = ?", (file,))
    root = Path(config.get_settings().pst_dir)
    f = Path(file)
    if f.is_file():
        dir_ = directory()
        _save([analyse_file(f, root, dir_, name_index(dir_), {})], replace=False)


def files() -> list[dict]:
    root = Path(config.get_settings().pst_dir)
    out = []
    for r in db.query("SELECT * FROM pst_files ORDER BY status != 'à valider', status != 'non attribué', email, file"):
        d = dict(r)
        d["candidates"] = json.loads(d["candidates"])
        d["content"] = json.loads(d["content"])
        d["name"] = Path(d["file"]).name
        try:
            d["folder"] = str(Path(d["file"]).parent.relative_to(root)).replace(".", "") or "(racine)"
        except ValueError:
            d["folder"] = str(Path(d["file"]).parent)
        out.append(d)
    return out


def ready_files(email: str) -> list[dict]:
    """PST validés (prêts) d'un compte, toujours présents sur disque."""
    return [dict(r) for r in db.query("SELECT * FROM pst_files WHERE email = ? AND status = ? ORDER BY file",
                                      (email, READY)) if Path(r["file"]).is_file()]


def ready_accounts() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in db.query("SELECT * FROM pst_files WHERE status = ? AND email IS NOT NULL ORDER BY email, file", (READY,)):
        out.setdefault(r["email"], []).append(dict(r))
    return out
