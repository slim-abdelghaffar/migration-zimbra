"""Moteur de migration par lot : simulation (aucune écriture) ou import réel.

Par compte : jeton délégué neuf -> GetInfoRequest -> concordance (seul blocage) ->
extraction java-libpst dans work/ -> analyse -> import -> comptage Zimbra -> nettoyage.
"""
import csv
import io
import json
import logging
import shutil
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db, inventory, mailbox, pst, state
from .zimbra import UserSession, ZimbraError, http_client

log = logging.getLogger("migration")

RUN_LOG_DIR = db.STATE_DIR / "logs" / "runs"
SIMULATION, REAL = "simulation", "réel"
FINAL = {"terminé", "terminé avec erreurs", "bloqué", "simulation OK", "refusé"}
RESUMABLE = {"interrompu", "erreur", "en attente", "en cours"}
BATCH = 50                 # éléments par import REST (ics / vcf)
MAIL_BATCH = 25            # mails par envoi groupé (upload multipart + BatchRequest)
MAIL_BATCH_BYTES = 8 << 20  # taille maximale d'un envoi groupé (les gros mails partent seuls)
MAX_CONSECUTIVE_ERRORS = 20

_active: dict[int, threading.Event] = {}
_active_lock = threading.Lock()


class Cancelled(Exception):
    pass


def empty_counts() -> dict:
    c = {k: {"pst": 0, "exclus": 0, "hors_periode": 0, "deja": 0, "a_importer": 0,
             "importes": 0, "erreurs": 0, "zimbra": None} for k in pst.KINDS}
    c["autres"] = {"non_pris_en_charge": 0, "illisibles": 0, "classes": {}}
    c["analyse"] = False  # vrai une fois le PST extrait et analysé
    return c


# --- Journal par compte --------------------------------------------------------------

class AccountLog:
    def __init__(self, run_id: int, email: str):
        d = RUN_LOG_DIR / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{email}.log"
        self.email = email

    def __call__(self, msg: str, level: str = "INFO") -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level} {msg}\n")
        if level != "INFO":
            log.warning("%s : %s", self.email, msg)


def log_path(run_id: int, email: str) -> Path:
    return RUN_LOG_DIR / str(run_id) / f"{email}.log"


# --- Envoi des mails --------------------------------------------------------------------

class MailSender:
    """Envoi des mails par lots, en parallèle.

    Chaque lot = 1 upload multipart + 1 BatchRequest de AddMsgRequest (2 allers-retours pour 25 mails au
    lieu de 50). Plusieurs lots partent en même temps pendant que les suivants sont préparés.
    Si le serveur refuse le mode groupé, repli automatique sur l'envoi mail par mail (toujours en parallèle).
    Les résultats sont enregistrés dans le fil principal (compteurs, idempotence, journal).
    """

    def __init__(self, job: "AccountJob", user: UserSession, workers: int):
        self.job, self.user = job, user
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"envoi-{job.email}")
        self.pending: deque = deque()
        self.max_inflight = workers * 2
        self.batch: list[dict] = []
        self.batch_bytes = 0
        self.grouped = True
        self.group_failures = 0
        self.consecutive = 0

    def add(self, entry: dict) -> None:
        size = len(entry["raw"])
        if size > MAIL_BATCH_BYTES:
            self._submit([entry])
            return
        if self.batch and (len(self.batch) >= MAIL_BATCH or self.batch_bytes + size > MAIL_BATCH_BYTES):
            self._submit(self.batch)
            self.batch, self.batch_bytes = [], 0
        self.batch.append(entry)
        self.batch_bytes += size

    def _submit(self, entries: list[dict]) -> None:
        while len(self.pending) >= self.max_inflight:
            self._drain_one()
        self.pending.append(self.pool.submit(self._send, entries))

    def _send(self, entries: list[dict]) -> list[tuple[dict, str | None, str | None]]:
        """Fil d'envoi : lot groupé si possible, sinon mail par mail."""
        if self.grouped and len(entries) > 1:
            try:
                aids = self.user.upload_many([e["raw"] for e in entries])
                res = self.user.add_msgs([(e["fid"], aid, e["flags"], e["date"]) for e, aid in zip(entries, aids)])
                return [(e, zid, err) for e, (zid, err) in zip(entries, res)]
            except Exception as ex:  # noqa: BLE001 — le lot est renvoyé mail par mail
                self.group_failures += 1
                if self.group_failures >= 2 and self.grouped:
                    self.grouped = False
                    self.job.log(f"Envoi groupé refusé par le serveur ({ex}) : passage à l'envoi mail par mail", "WARN")
        out = []
        for e in entries:
            try:
                aid = self.user.upload(e["raw"])
                out.append((e, self.user.add_msg(e["fid"], aid, e["flags"], e["date"]), None))
            except Exception as ex:  # noqa: BLE001
                out.append((e, None, str(ex)))
        return out

    def _drain_one(self) -> None:
        self._handle(self.pending.popleft().result())

    def _handle(self, results: list) -> None:
        job = self.job
        ok: dict[str, list] = {}
        for e, zid, err in results:
            job.done += 1
            if err is None:
                ok.setdefault(e["tkey"], []).append((e["key"], zid))
                job.counts["mail"]["importes"] += 1
                self.consecutive = 0
            else:
                job.counts["mail"]["erreurs"] += 1
                job.log(f"{e['label']} : {err}", "ERROR")
                self.consecutive += 1
        for tkey, keys in ok.items():
            db.mark_imported(job.email, "mail", tkey, keys, job.run_id)
        job.progress()
        if self.consecutive >= MAX_CONSECUTIVE_ERRORS:
            raise RuntimeError(f"{MAX_CONSECUTIVE_ERRORS} erreurs consécutives, compte arrêté")

    def finish(self) -> None:
        """Envoie le dernier lot et attend la fin de tous les envois."""
        try:
            if self.batch:
                self._submit(self.batch)
                self.batch, self.batch_bytes = [], 0
            while self.pending:
                self._drain_one()
        finally:
            self.close()

    def close(self) -> None:
        """Arrêt : les envois déjà partis sont terminés et enregistrés (idempotence), rien de nouveau ne part."""
        self.batch = []
        while self.pending:
            try:
                self._drain_one()
            except Exception:  # noqa: BLE001
                pass
        self.pool.shutdown(wait=True)


# --- Traitement d'un compte ------------------------------------------------------------

class AccountJob:
    def __init__(self, run_id: int, email: str, mode: str, since: str | None, cancel: threading.Event):
        self.run_id, self.email, self.mode, self.since = run_id, email, mode, since
        self.cancel = cancel
        self.settings = config.get_settings()
        self.log = AccountLog(run_id, email)
        self.counts = empty_counts()
        self.work = pst.WORK_DIR / f"run{run_id}-{email}"
        self.src = ""            # nom du PST en cours (fait partie de l'identifiant des éléments)
        self.prior: set = set()  # éléments déjà planifiés dans les PST précédents du même compte
        self._seen: set = set()
        self._last_update = 0.0
        self.done = 0
        self.since_ms = None
        if since:
            d = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=ZoneInfo(self.settings.timezone))
            self.since_ms = int(d.timestamp() * 1000)

    def update(self, **fields) -> None:
        db.update_account(self.run_id, self.email, **fields)

    def progress(self, force: bool = False) -> None:
        if force or time.monotonic() - self._last_update > 2:
            self._last_update = time.monotonic()
            self.update(done=self.done, counts=self.counts)

    def phase(self, step: str, label: str) -> None:
        """Étape courante (jeton, concordance, extraction, analyse, import, verification) + libellé."""
        self.counts["step"] = step
        if step == "import":
            self.counts["t_import"] = time.time()
        self.update(phase=label, counts=self.counts)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def run(self) -> None:
        self.update(status="en cours", phase="démarrage", started=time.time(), error=None, done=0, total=0)
        self.log(f"=== Lot {self.run_id}, mode {self.mode}" + (f", depuis {self.since}" if self.since else ""))
        try:
            self._run()
        except Cancelled:
            self.log("Arrêt demandé : compte interrompu", "WARN")
            self.update(status="interrompu", phase="", counts=self.counts)
        except Exception as e:  # noqa: BLE001
            self.log(f"Erreur : {type(e).__name__} : {e}", "ERROR")
            self.update(status="erreur", phase="", error=f"{type(e).__name__} : {e}"[:500], counts=self.counts)
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
            self.update(finished=time.time(), done=self.done)

    def _run(self) -> None:
        files = inventory.ready_files(self.email)
        if not files:
            self.log("Aucun PST validé (prêt) pour ce compte dans l'inventaire : aucune action", "ERROR")
            self.update(status="refusé", phase="", error="aucun PST validé pour ce compte dans l'inventaire")
            return
        self.log("PST attribués : " + ", ".join(f"{Path(f['file']).name} ({f['method']})" for f in files))

        admin = state.admin_session()
        with http_client(self.settings.ca_file) as client:
            user = UserSession(admin, self.email, client)
            self.phase("jeton", "jeton délégué")
            user.renew()
            info = user.get_info()
            self.phase("concordance", "concordance")
            token_account = (info.get("name") or "").lower()
            self.update(token_account=info.get("name"))
            self.log(f"DelegateAuthRequest OK, GetInfoRequest : compte {info.get('name')}")
            if token_account != self.email:
                msg = f"Concordance KO : PST attribué à {self.email}, jeton {info.get('name')}"
                self.log(msg + " — import bloqué", "ERROR")
                self.update(status="bloqué", phase="", error=msg)
                return
            self.log("Concordance OK (compte attribué au PST = compte du jeton)")
            self.check_cancel()

            # État réel de la boîte : ce qui a été supprimé dans Zimbra depuis le dernier import sera réimporté
            self.phase("concordance", "vérification de la boîte Zimbra")
            try:
                mailbox.resync(self.email, user, self.log)
            except ZimbraError as e:
                self.log(f"Vérification de la boîte impossible ({e}) : l'historique local est conservé", "WARN")
            self.check_cancel()

            biggest = max(Path(f["file"]).stat().st_size for f in files)
            pst.WORK_DIR.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(pst.WORK_DIR).free
            need = 2 * biggest + 512 * 1024 * 1024
            if free < need:
                raise RuntimeError(f"espace disque insuffisant dans {pst.WORK_DIR} : {free >> 20} Mo libres, "
                                   f"{need >> 20} Mo nécessaires")

            sender, errors, total = None, 0, 0
            for i, f in enumerate(files, 1):
                pst_file = Path(f["file"])
                tag = f" — PST {i}/{len(files)}" if len(files) > 1 else ""
                self.src = pst_file.name
                self.work = pst.WORK_DIR / f"run{self.run_id}-{self.email}-{i}"
                shutil.rmtree(self.work, ignore_errors=True)

                self.phase("extraction", f"extraction{tag}")
                self.log(f"Extraction java-libpst de {pst_file.name} ({pst_file.stat().st_size >> 20} Mo){tag}")
                items = pst.extract(pst_file, self.work, self.settings.timezone,
                                    lambda n, tag=tag: self.update(phase=f"extraction{tag} ({n} éléments)"),
                                    self.cancel.is_set)
                self.check_cancel()

                self.phase("analyse", f"analyse{tag}")
                before = sum(self.counts[k]["a_importer"] for k in pst.KINDS)
                found = self.analyse(items)
                if sender is None or sender.startswith("absent"):
                    sender = found
                self.update(sender_found=sender, counts=self.counts)
                todo = sum(self.counts[k]["a_importer"] for k in pst.KINDS) - before

                if self.mode == REAL:
                    total += todo
                    self.update(total=total)
                    self.phase("import", f"import{tag}")
                    errors = self.import_all(user, items)
                self.prior |= self._seen
                shutil.rmtree(self.work, ignore_errors=True)

            self.log_summary()
            if self.mode == SIMULATION:
                total_todo = sum(self.counts[k]["a_importer"] for k in pst.KINDS)
                self.log(f"Simulation terminée : {total_todo} éléments seraient importés (aucune écriture)")
                self.update(status="simulation OK", phase="", counts=self.counts)
                return
            status = "terminé" if errors == 0 else "terminé avec erreurs"
            self.log(f"Import terminé : {status}")
            self.update(status=status, phase="", counts=self.counts)

    # --- Analyse (aucune écriture) ---------------------------------------------------

    def plan(self, items: Path):
        """Parcourt items.jsonl ; produit (élément, cible, clé) pour chaque élément à importer."""
        fmap = self._plan_fmap = pst.FolderMap()
        already = db.imported_keys(self.email)
        seen: set = set(self.prior)
        for it in pst.read_items(items):
            kind = it["kind"]
            if kind == "folder":
                fmap.add(it)
                continue
            if kind not in pst.KINDS:
                yield fmap, it, None, None
                continue
            it["src"] = self.src
            target, why = fmap.route(it)
            if why:
                yield fmap, it, None, why
                continue
            if kind == "mail" and self.since_ms:
                d = pst.mail_date_ms(it, target.base_id == "5")
                if d and d < self.since_ms:
                    yield fmap, it, target, "hors_periode"
                    continue
            key = pst.mail_key(it, self.email) if kind == "mail" else pst.item_uid(self.email, it["node"], self.src)
            ident = (kind, target.key, key)
            if ident in already or ident in seen:
                yield fmap, it, target, "deja"
                continue
            seen.add(ident)
            self._seen = seen
            yield fmap, it, target, key

    def analyse(self, items: Path):
        senders = Counter()
        for fmap, it, target, key in self.plan(items):
            kind = it["kind"]
            if kind not in pst.KINDS:
                o = self.counts["autres"]
                if kind == "error":
                    o["illisibles"] += 1
                    self.log(f"Élément illisible dans {'/'.join(it.get('path', []))} : {it.get('error')}", "WARN")
                elif kind in ("other", "distlist"):
                    o["non_pris_en_charge"] += 1
                    cls = it.get("class") or kind
                    o["classes"][cls] = o["classes"].get(cls, 0) + 1
                continue
            c = self.counts[kind]
            c["pst"] += 1
            if target is None:
                c["exclus"] += 1
            elif key in ("hors_periode", "deja"):
                c[key] += 1
            else:
                c["a_importer"] += 1
            if kind == "mail" and target is not None and target.base_id == "5" and not target.segments:
                if it.get("sender_smtp"):
                    senders[it["sender_smtp"].lower()] += 1
        for f in (fmap.folders.values() if fmap else []):
            dest = f["excluded"] and f"exclu ({f['excluded']})" or (f["target"].key if f["target"] else "racine PST")
            self.log(f"Dossier PST {'/'.join(f['path'])} [{f['class'] or '-'}, {f['count']}] -> {dest}")
        self.fmap = self._plan_fmap  # carte complète des dossiers, réutilisée pour l'import
        self.counts["analyse"] = True
        sender = senders.most_common(1)[0][0] if senders else "absent (pas d'éléments envoyés)"
        return sender

    def log_summary(self) -> None:
        for k in pst.KINDS:
            c = self.counts[k]
            if c["pst"] == 0:
                self.log(f"{pst.KIND_LABELS[k]} : absent du PST")
            else:
                self.log(f"{pst.KIND_LABELS[k]} : {c['pst']} dans le PST, {c['a_importer']} à importer, "
                         f"{c['deja']} déjà importés, {c['exclus']} exclus, {c['hors_periode']} hors période")
        o = self.counts["autres"]
        if o["non_pris_en_charge"] or o["illisibles"]:
            self.log(f"Non migrés : {o['non_pris_en_charge']} non pris en charge {o['classes']}, "
                     f"{o['illisibles']} illisibles", "WARN")

    # --- Import réel -------------------------------------------------------------------

    def import_all(self, user: UserSession, items: Path) -> int:
        folders: dict[str, tuple[str, str]] = {}  # clé cible -> (id Zimbra, chemin absolu)
        used: dict[str, set[str]] = {k: set() for k in pst.KINDS}
        batches: dict[tuple[str, str], list] = {}
        base = self.work

        def ensure(target: pst.Target) -> tuple[str, str]:
            if target.key in folders:
                return folders[target.key]
            fid, path = target.base_id, "/" + pst.SYSTEM_PATHS[target.base_id]
            for i, (name, view) in enumerate(target.segments):
                sub = pst.Target(target.base_id, target.segments[: i + 1])
                if sub.key in folders:
                    fid, path = folders[sub.key]
                    continue
                fid, path = user.create_folder(name, fid, view)
                folders[sub.key] = (fid, path)
                self.log(f"Dossier Zimbra {path} (id {fid})")
            folders[target.key] = (fid, path)
            return fid, path

        def flush(kind: str, tkey: str) -> None:
            batch = batches.pop((kind, tkey), [])
            if not batch:
                return
            fid, path = folders[tkey]
            try:
                self._rest_batch(user, kind, path, batch)
                db.mark_imported(self.email, kind, tkey, [(k, None) for k, _ in batch], self.run_id)
                self.counts[kind]["importes"] += len(batch)
            except ZimbraError as e:
                self.log(f"Import groupé {pst.KIND_LABELS[kind]} vers {path} en échec ({e}), reprise élément par élément", "WARN")
                for k, payload in batch:
                    try:
                        self._rest_batch(user, kind, path, [(k, payload)])
                        db.mark_imported(self.email, kind, tkey, [(k, None)], self.run_id)
                        self.counts[kind]["importes"] += 1
                    except ZimbraError as e2:
                        self.counts[kind]["erreurs"] += 1
                        self.log(f"{pst.KIND_LABELS[kind]} {k} : {e2}", "ERROR")
            self.progress()

        # Arborescence complète (y compris dossiers vides), avant les éléments (carte issue de l'analyse)
        for f in self.fmap.folders.values():
            if f["target"]:
                ensure(f["target"])
                self.check_cancel()

        sender = MailSender(self, user, self.settings.connections)
        try:
            self._import_items(user, items, ensure, used, batches, flush, sender, base)
            sender.finish()
        finally:
            sender.close()

        for kind, tkey in list(batches):
            self.check_cancel()
            flush(kind, tkey)

        self.phase("verification", "comptage Zimbra")
        for kind in pst.KINDS:
            if used[kind]:
                self.counts[kind]["zimbra"] = sum(user.folder_count(fid) for fid in used[kind])
        self.progress(force=True)
        return sum(self.counts[k]["erreurs"] for k in pst.KINDS)

    def _import_items(self, user, items, ensure, used, batches, flush, sender, base) -> None:
        consecutive = 0
        for _, it, target, key in self.plan(items):
            kind = it["kind"]
            if kind not in pst.KINDS or target is None:
                continue
            self.check_cancel()
            fid, path = ensure(target)
            used[kind].add(fid)  # pour le comptage Zimbra, même si rien n'est à importer
            if key in ("hors_periode", "deja"):
                continue
            warn = lambda m, it=it: self.log(f"{self._label(it)} : {m}", "WARN")  # noqa: E731
            try:
                if kind == "mail":
                    raw = pst.build_mime(it, base, self.email, warn)
                    sender.add({"raw": raw, "key": key, "tkey": target.key, "fid": fid, "label": self._label(it),
                                "flags": pst.mail_flags(it, target),
                                "date": pst.mail_date_ms(it, target.base_id == "5" and not target.segments)})
                    continue  # compté à la fin de l'envoi (MailSender._handle)
                else:
                    if kind == "appt":
                        payload = pst.vevent(it, self.email, warn)
                    elif kind == "task":
                        payload = (pst.vtodo(it, self.email), [])
                    else:
                        payload = pst.vcard(it, self.email)
                    batches.setdefault((kind, target.key), []).append((key, payload))
                    if len(batches[(kind, target.key)]) >= BATCH:
                        flush(kind, target.key)
                consecutive = 0
            except ZimbraError as e:
                consecutive += 1
                self.counts[kind]["erreurs"] += 1
                self.log(f"{self._label(it)} : {e}", "ERROR")
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(f"{MAX_CONSECUTIVE_ERRORS} erreurs consécutives, compte arrêté (dernière : {e})")
            except Exception as e:  # noqa: BLE001 — contenu PST inattendu : l'élément est compté en erreur, on continue
                self.counts[kind]["erreurs"] += 1
                self.log(f"{self._label(it)} : conversion impossible : {type(e).__name__} : {e}", "ERROR")
            self.done += 1
            self.progress()

    @staticmethod
    def _rest_batch(user: UserSession, kind: str, path: str, batch: list) -> None:
        if kind == "contact":
            user.rest_import(path, "vcf", b"".join(p for _, p in batch), "text/x-vcard; charset=utf-8")
        else:
            data = pst.vcalendar([p[0] for _, p in batch], [p[1] for _, p in batch])
            user.rest_import(path, "ics", data, "text/calendar; charset=utf-8")

    @staticmethod
    def _label(it: dict) -> str:
        what = {"mail": "Mail", "appt": "Rendez-vous", "contact": "Contact", "task": "Tâche"}.get(it["kind"], "Élément")
        name = it.get("subject") or it.get("display") or ""
        ident = it.get("msgid") or f"nœud {it.get('node')}"
        return f"{what} « {pst.clean(name)[:80]} » ({ident})"


# --- Lots ----------------------------------------------------------------------------

def active_emails() -> set[str]:
    with _active_lock:
        ids = list(_active)
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["email"] for r in db.query(f"SELECT email FROM run_accounts WHERE run_id IN ({marks})", tuple(ids))}


def start(emails: list[str], mode: str, since: str | None) -> int:
    err = pst.extractor_ready()
    if err:
        raise RuntimeError(err)
    busy = active_emails() & set(emails)
    if busy:
        raise RuntimeError("Comptes déjà en cours dans un autre lot : " + ", ".join(sorted(busy)))
    rows = []
    for e in emails:
        files = inventory.ready_files(e)
        if not files:
            raise RuntimeError(f"{e} n'a aucun PST validé (prêt) dans l'inventaire")
        rows.append({"email": e, "file": " ; ".join(Path(f["file"]).name for f in files)})
    run_id = db.execute("INSERT INTO runs(created, mode, since, status) VALUES(?,?,?,?)",
                        (time.time(), mode, since, "en cours"))
    db.executemany("INSERT INTO run_accounts(run_id, email, file, status) VALUES(?,?,?,?)",
                   [(run_id, r["email"], r["file"], "en attente") for r in rows])
    _launch(run_id, [r["email"] for r in rows], mode, since)
    return run_id


def resume(run_id: int) -> None:
    run = db.query("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise RuntimeError("Lot inconnu")
    with _active_lock:
        if run_id in _active:
            raise RuntimeError("Lot déjà en cours")
    err = pst.extractor_ready()
    if err:
        raise RuntimeError(err)
    todo = [r["email"] for r in db.query("SELECT email, status FROM run_accounts WHERE run_id = ?", (run_id,))
            if r["status"] in RESUMABLE]
    if not todo:
        raise RuntimeError("Aucun compte à reprendre dans ce lot")
    busy = active_emails() & set(todo)
    if busy:
        raise RuntimeError("Comptes déjà en cours dans un autre lot : " + ", ".join(sorted(busy)))
    for e in todo:
        db.update_account(run_id, e, status="en attente", error=None, phase="")
    db.execute("UPDATE runs SET status = 'en cours', finished = NULL WHERE id = ?", (run_id,))
    _launch(run_id, todo, run[0]["mode"], run[0]["since"])


def stop(run_id: int) -> None:
    with _active_lock:
        ev = _active.get(run_id)
    if ev:
        ev.set()


def is_active(run_id: int) -> bool:
    with _active_lock:
        return run_id in _active


def _launch(run_id: int, emails: list[str], mode: str, since: str | None) -> None:
    cancel = threading.Event()
    with _active_lock:
        _active[run_id] = cancel
    parallelism = config.get_settings().parallelism

    def worker(email: str) -> None:
        if cancel.is_set():
            db.update_account(run_id, email, status="interrompu")
            return
        AccountJob(run_id, email, mode, since, cancel).run()

    def run_all() -> None:
        try:
            with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix=f"lot{run_id}") as ex:
                list(ex.map(worker, emails))
        finally:
            statuses = {r["status"] for r in db.query("SELECT status FROM run_accounts WHERE run_id = ?", (run_id,))}
            final = "interrompu" if statuses & RESUMABLE else "terminé"
            db.execute("UPDATE runs SET status = ?, finished = ? WHERE id = ?", (final, time.time(), run_id))
            with _active_lock:
                _active.pop(run_id, None)
            log.info("Lot %d %s", run_id, final)

    threading.Thread(target=run_all, name=f"lot{run_id}", daemon=True).start()
    log.info("Lot %d lancé (%s, %d comptes, parallélisme %d)", run_id, mode, len(emails), parallelism)


def recover_on_startup() -> None:
    """Après un redémarrage : les lots en cours deviennent « interrompu » (reprise possible)."""
    db.execute("UPDATE runs SET status = 'interrompu' WHERE status = 'en cours'")
    db.execute("UPDATE run_accounts SET status = 'interrompu', phase = '' WHERE status IN ('en cours', 'en attente')")
    for d in pst.WORK_DIR.glob("run*-*"):
        shutil.rmtree(d, ignore_errors=True)


# --- Rapport -------------------------------------------------------------------------

def counts_of(row) -> dict:
    c = empty_counts()
    c.update(json.loads(row["counts"] or "{}"))
    return c


def report_csv(run_id: int) -> str:
    run = db.query("SELECT * FROM runs WHERE id = ?", (run_id,))[0]
    real = run["mode"] == REAL
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    head = ["lot", "mode", "pst", "compte_jeton", "expediteur_trouve", "statut", "erreur"]
    for k in pst.KINDS:
        lab = pst.KIND_LABELS[k].lower().replace("â", "a")
        head += [f"{lab}_pst", f"{lab}_{'importes' if real else 'a_importer'}", f"{lab}_deja_importes",
                 f"{lab}_exclus", f"{lab}_hors_periode", f"{lab}_erreurs", f"{lab}_zimbra"]
    head += ["non_migres", "illisibles"]
    w.writerow(head)
    for r in db.query("SELECT * FROM run_accounts WHERE run_id = ? ORDER BY email", (run_id,)):
        c = counts_of(r)
        row = [run_id, run["mode"], Path(r["file"]).name, r["token_account"] or "", r["sender_found"] or "",
               r["status"], r["error"] or ""]
        for k in pst.KINDS:
            x = c[k]
            if not c["analyse"]:
                row += ["non analysé"] + [""] * 6
            elif x["pst"] == 0:
                row += ["absent"] + [""] * 6
            else:
                row += [x["pst"], x["importes"] if real else x["a_importer"], x["deja"], x["exclus"],
                        x["hors_periode"], x["erreurs"], "" if x["zimbra"] is None else x["zimbra"]]
        row += [c["autres"]["non_pris_en_charge"], c["autres"]["illisibles"]]
        w.writerow(row)
    return "﻿" + buf.getvalue()
