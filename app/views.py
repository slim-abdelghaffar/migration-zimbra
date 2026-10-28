"""Données des écrans : tableau de bord et suivi en temps réel d'un lot."""
import re
import time
from collections import deque
from pathlib import Path

from . import db, inventory, mailbox, migration, pst

STEPS = [("jeton", "Jeton délégué"), ("concordance", "Concordance"), ("extraction", "Extraction PST"),
         ("analyse", "Analyse"), ("import", "Import"), ("verification", "Vérification")]
STEP_KEYS = [k for k, _ in STEPS]
OK_STATUSES = {"terminé", "simulation OK"}
KO_STATUSES = {"bloqué", "erreur", "refusé", "terminé avec erreurs"}


def tone(status: str) -> str:
    if status in OK_STATUSES:
        return "ok"
    if status == "terminé avec erreurs":
        return "warn"
    if status in KO_STATUSES:
        return "ko"
    if status == "en cours":
        return "run"
    return "idle"


def _steps(status: str, step: str | None, mode: str) -> list[dict]:
    """État de chaque étape : done / current / failed / skipped / todo."""
    idx = STEP_KEYS.index(step) if step in STEP_KEYS else (-1 if status in ("en attente", "refusé") else 0)
    out = []
    for i, (key, label) in enumerate(STEPS):
        if mode == migration.SIMULATION and key in ("import", "verification"):
            st = "skipped"
        elif status in OK_STATUSES or (status == "terminé avec erreurs"):
            st = "done"
        elif i < idx:
            st = "done"
        elif i == idx:
            st = {"en cours": "current", "bloqué": "failed", "erreur": "failed",
                  "interrompu": "paused"}.get(status, "todo")
        else:
            st = "todo"
        out.append({"key": key, "label": label, "state": st})
    return out


def _tail(path: Path, n: int = 8) -> list[str]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return [l.rstrip("\n")[20:] if re.match(r"\d{4}-", l) else l.rstrip("\n") for l in deque(f, n)]


def account_view(row, mode: str, run_id: int) -> dict:
    c = migration.counts_of(row)
    status, now = row["status"], time.time()
    done, total = row["done"] or 0, row["total"] or 0
    m = re.search(r"\((\d+) éléments\)", row["phase"] or "")
    extracted = int(m.group(1)) if m else None
    rate = eta = None
    if c.get("t_import") and done and status == "en cours":
        elapsed = max(1.0, now - c["t_import"])
        rate = done / elapsed * 60
        eta = (total - done) / (done / elapsed) if total > done else 0
    if status in OK_STATUSES or status == "terminé avec erreurs":
        pct = 100
    elif total:
        pct = round(100 * done / total)
    else:
        pct = None
    kinds = []
    for k in pst.KINDS:
        x = c[k]
        kinds.append({"key": k, "label": pst.KIND_LABELS[k], "analysed": c.get("analyse", False),
                      "absent": c.get("analyse", False) and x["pst"] == 0, **x})
    return {
        "email": row["email"], "status": status, "tone": tone(status), "phase": row["phase"] or "",
        "steps": _steps(status, c.get("step"), mode), "done": done, "total": total, "pct": pct,
        "extracted": extracted, "rate": rate, "eta": eta, "token_account": row["token_account"],
        "sender_found": row["sender_found"], "error": row["error"], "kinds": kinds,
        "others": c["autres"], "started": row["started"], "finished": row["finished"],
        "log": _tail(migration.log_path(run_id, row["email"])),
    }


def run_view(run_id: int) -> dict | None:
    rows = db.query("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not rows:
        return None
    run = dict(rows[0])
    accounts = [account_view(a, run["mode"], run_id) for a in
                db.query("SELECT * FROM run_accounts WHERE run_id = ? ORDER BY email", (run_id,))]
    done = sum(a["done"] for a in accounts)
    total = sum(a["total"] for a in accounts)
    finished = sum(a["status"] in OK_STATUSES | KO_STATUSES for a in accounts)
    real = run["mode"] == migration.REAL
    imported = sum(k["importes" if real else "a_importer"] for a in accounts for k in a["kinds"])
    errors = sum(k["erreurs"] for a in accounts for k in a["kinds"])
    active = migration.is_active(run_id)
    return {
        "id": run_id, "mode": run["mode"], "real": real, "since": run["since"], "status": run["status"],
        "created": run["created"], "active": active,
        "resumable": not active and any(a["status"] in migration.RESUMABLE for a in accounts),
        "accounts": accounts, "n_accounts": len(accounts), "finished": finished,
        "ok": sum(a["status"] in OK_STATUSES for a in accounts),
        "ko": sum(a["status"] in KO_STATUSES for a in accounts),
        "pct": round(100 * finished / len(accounts)) if accounts else 0,
        "items_done": done, "items_total": total, "imported": imported, "errors": errors,
        "rate": sum(a["rate"] or 0 for a in accounts) or None,
    }


# Catégories d'avancement d'un compte : (clé, libellé, couleur, icône)
CATEGORIES = [
    ("migre", "Migrés", "Migré", "#0ca30c", "check"),
    ("en_cours", "En cours", "En cours", "#00a0df", "activity"),
    ("a_migrer", "À migrer", "À migrer", "#8a9bb8", "send"),
    ("a_valider", "PST à valider", "PST à valider", "#fab219", "alert"),
    ("echec", "En échec", "En échec", "#d03b3b", "x"),
    ("sans_pst", "Sans PST", "Sans PST", "#d6dde6", "archive"),
]
CAT = {k: {"key": k, "label": l, "one": o, "color": c, "icon": i} for k, l, o, c, i in CATEGORIES}
FAILED = {"bloqué", "erreur", "refusé", "interrompu"}


def accounts_progress() -> dict:
    """Croise annuaire Zimbra, PST attribués et lots : statut d'avancement de chaque compte."""
    dir_ = inventory.directory()
    files = [dict(r) for r in db.query("SELECT * FROM pst_files")]
    ready, proposed = {}, {}
    for f in files:
        if f["email"] and f["status"] == inventory.READY:
            ready.setdefault(f["email"], []).append(f)
        elif f["email"] and f["status"] == inventory.TO_VALIDATE:
            proposed.setdefault(f["email"], []).append(f)
    last_real, last_sim = {}, {}
    for r in db.query("""SELECT a.*, r.mode, r.created FROM run_accounts a JOIN runs r ON r.id = a.run_id
                         ORDER BY a.run_id"""):
        (last_real if r["mode"] == migration.REAL else last_sim)[r["email"]] = dict(r)
    imported = {r["email"]: r["n"] for r in db.query("SELECT email, COUNT(*) AS n FROM imported GROUP BY email")}
    active = migration.active_emails()
    usage = mailbox.usage()
    checks = {r["email"]: dict(r) for r in db.query("SELECT email, checked_at, removed FROM mailbox_check")}

    rows = []
    for email, acc in dir_.items():
        rf, lr = ready.get(email, []), last_real.get(email)
        if email in active:
            cat = "en_cours"
        elif lr and lr["status"] in ("terminé", "terminé avec erreurs"):
            done_names = set((lr["file"] or "").split(" ; "))
            ck = checks.get(email)
            emptied = bool(ck and ck["removed"] and ck["checked_at"] > (lr["finished"] or 0))
            u = usage.get(email)
            # boîte vide dans Zimbra alors qu'elle a été migrée : à réimporter (détecté par la taille, lue par l'admin)
            emptied = emptied or bool(u and u["used"] == 0 and imported.get(email) and u["at"] > (lr["finished"] or 0))
            cat = "a_migrer" if emptied or any(Path(f["file"]).name not in done_names for f in rf) else "migre"
        elif lr and lr["status"] in FAILED and rf:
            cat = "echec"
        elif rf:
            cat = "a_migrer"
        elif proposed.get(email):
            cat = "a_valider"
        else:
            cat = "sans_pst"
        pf = rf or proposed.get(email, [])
        rows.append({
            "email": email, "name": acc["display_name"], "zstatus": acc["status"], "cat": cat,
            "pst": len(pf), "size": sum(f["size"] for f in pf),
            "pst_names": [Path(f["file"]).name for f in pf],
            "run": lr and {"id": lr["run_id"], "status": lr["status"], "date": lr["created"]},
            "simulated": email in last_sim and last_sim[email]["status"] == "simulation OK",
            "imported": imported.get(email, 0),
            "usage": usage.get(email),
            "modified": cat == "a_migrer" and bool(lr) and lr["status"] in ("terminé", "terminé avec erreurs")
                        and (bool(checks.get(email, {}).get("removed")) or bool(usage.get(email) and usage[email]["used"] == 0)),
        })
    counts = {k: 0 for k in CAT}
    for r in rows:
        counts[r["cat"]] += 1
    total = len(rows)
    with_pst = total - counts["sans_pst"]
    orphans = [f for f in files if f["status"] in (inventory.UNASSIGNED, inventory.NOT_FOUND, inventory.UNCHECKED)]
    invalid = [f for f in files if f["status"] == inventory.INVALID]
    return {
        "rows": rows, "total": total, "with_pst": with_pst, "counts": counts,
        "categories": [dict(CAT[k], n=counts[k], pct=round(100 * counts[k] / total, 1) if total else 0) for k in CAT],
        # Périmètre de migration = comptes ayant au moins un PST ; les comptes sans PST sont hors périmètre
        "scope": [dict(CAT[k], n=counts[k], pct=round(100 * counts[k] / with_pst, 1) if with_pst else 0)
                  for k in CAT if k != "sans_pst"],
        "remaining": with_pst - counts["migre"],
        "remaining_all": total - counts["migre"],
        "coverage": round(100 * with_pst / total, 1) if total else 0,
        "modified_n": sum(r["modified"] for r in rows),
        "rate": round(100 * counts["migre"] / with_pst, 1) if with_pst else 0,
        "rate_all": round(100 * counts["migre"] / total, 1) if total else 0,
        "pst_files": len(files), "pst_size": sum(f["size"] for f in files),
        "to_validate": sum(f["status"] == inventory.TO_VALIDATE for f in files),
        "orphans": len(orphans),
        "invalid": len(invalid),
        "imported_total": sum(imported.values()),
        "synced_at": inventory.directory_synced_at(),
        "job": inventory.job_state(),
        "verify": mailbox.job_state(),
        "usage_total": sum(u["used"] for u in usage.values()),
        "active": bool(active),
    }


def dashboard() -> dict:
    d = accounts_progress()
    d["runs"] = db.query("""SELECT r.*, COUNT(a.email) AS n,
                              SUM(a.status IN ('terminé', 'simulation OK')) AS ok,
                              SUM(a.status IN ('bloqué', 'erreur', 'refusé', 'terminé avec erreurs')) AS ko
                       FROM runs r LEFT JOIN run_accounts a ON a.run_id = r.id
                       GROUP BY r.id ORDER BY r.id DESC LIMIT 5""")
    return d
