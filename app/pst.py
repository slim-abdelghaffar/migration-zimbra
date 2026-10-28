"""Extraction PST (java-libpst) et conversion vers les formats importables par Zimbra.

- extract() lance l'extracteur Java (lecture seule) dans un dossier de work/.
- FolderMap décide où va chaque dossier PST dans Zimbra (ou s'il est exclu).
- build_mime / vevent / vcard / vtodo produisent le contenu à importer.
"""
import hashlib
import json
import re
import shutil
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from email import policy
from email.header import Header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr
from mimetypes import guess_type
from pathlib import Path
from typing import Callable, Iterator

from .db import BASE_DIR, STATE_DIR

WORK_DIR = STATE_DIR / "work"
JAR = BASE_DIR / "vendor" / "java-libpst-0.9.3.jar"
CLASSES = BASE_DIR / "tools" / "build"

KINDS = ("mail", "appt", "contact", "task")
KIND_LABELS = {"mail": "Mails", "appt": "Calendrier", "contact": "Contacts", "task": "Tâches"}
KIND_VIEW = {"mail": "message", "appt": "appointment", "contact": "contact", "task": "task"}

# Dossiers système Zimbra : id -> chemin
SYSTEM_PATHS = {"1": "", "2": "Inbox", "3": "Trash", "4": "Junk", "5": "Sent", "6": "Drafts",
                "7": "Contacts", "10": "Calendar", "15": "Tasks"}
DEFAULT_FOLDER = {"mail": "2", "appt": "10", "contact": "7", "task": "15"}

# Noms Outlook (FR/EN) des dossiers de premier niveau -> dossier système, par vue
SPECIAL = {
    "message": {"boîte de réception": "2", "inbox": "2", "éléments envoyés": "5", "sent items": "5",
                "brouillons": "6", "drafts": "6", "éléments supprimés": "3", "deleted items": "3",
                "courrier indésirable": "4", "junk e-mail": "4", "junk email": "4"},
    "appointment": {"calendrier": "10", "calendar": "10"},
    "contact": {"contacts": "7"},
    "task": {"tâches": "15", "tasks": "15"},
}
# Dossiers techniques Outlook de premier niveau jamais migrés (avec leurs sous-dossiers)
EXCLUDED_NAMES = {
    "problèmes de synchronisation", "sync issues", "racine de yammer", "yammer root", "paramètres d'action rapide",
    "quick step settings", "paramètres des actions de conversation", "conversation action settings",
    "externalcontacts", "personmetadata", "recoverable items", "éléments récupérables", "search root",
    "finder", "freebusy data", "boîte d'envoi", "outbox",
}
EXCLUDED_CLASS_PREFIXES = ("IPF.CONFIGURATION", "IPF.CONTACT.MOC", "IPF.CONTACT.GALCONTACTS",
                           "IPF.CONTACT.RECIPIENTCACHE", "IPF.CONTACT.ORGANIZATIONALCONTACTS",
                           "IPF.CONTACT.COMPANY", "IPF.NOTE.OUTLOOKHOMEPAGE", "IPF.SKYPETEAMS", "IPF.FILES",
                           "IPF.NOTE.SOCIALCONNECTOR")
# Sous-dossiers techniques créés par Exchange Online, à n'importe quel niveau
EXCLUDED_ANY_LEVEL = {"recipient cache", "peoplecentricconversation buddies", "organizational contacts",
                      "companies", "gal contacts", "social activity notifications", "files", "yammer root"}
TOP_PREFIXES = ("top of ", "partie supérieure", "haut de la", "racine du fichier")
SEARCH_FOLDER_TYPE = 2


def norm(name: str) -> str:
    return unicodedata.normalize("NFC", (name or "").replace("’", "'")).strip().casefold()


def clean(s: str | None) -> str:
    """Retire les substituts UTF-16 isolés et les NUL venant du PST."""
    if not s:
        return ""
    return re.sub(r"[\ud800-\udfff]", "\ufffd", s).replace("\x00", "")


def zimbra_name(name: str) -> str:
    """Nom de dossier acceptable par Zimbra (pas de / : " ni caractères de contrôle)."""
    n = re.sub(r'[/:"\x00-\x1f\x7f]', "_", clean(name)).strip()
    return (n or "_")[:128]


def item_uid(email: str, node: int, src: str = "") -> str:
    """Identifiant stable d'un élément : compte + fichier PST d'origine + nœud dans le PST."""
    return "pst-" + hashlib.sha1(f"{email}:{src}:{node}".encode()).hexdigest()


# --- Extraction ----------------------------------------------------------------------

class ExtractError(Exception):
    pass


PST_MAGIC = b"!BDN"
RECENT_S = 60   # fichier modifié récemment : on vérifie s'il grossit encore
WATCH_S = 3     # durée d'observation de la taille


def is_copying(f: Path) -> bool:
    """Vrai si le fichier est en cours d'écriture (sa taille change pendant l'observation)."""
    st = f.stat()
    if time.time() - st.st_mtime > RECENT_S:
        return False
    time.sleep(WATCH_S)
    st2 = f.stat()
    return st2.st_size != st.st_size or st2.st_mtime != st.st_mtime


def check_pst_file(f: Path) -> str | None:
    """None si le fichier est un PST valide et complet, sinon la raison (lisible) du refus."""
    if is_copying(f):
        return "copie en cours"
    if f.stat().st_size == 0:
        return "fichier vide (0 octet) : copie incomplète ou fichier mal exporté"
    with open(f, "rb") as fh:
        head = fh.read(4)
    if head != PST_MAGIC:
        return "ce n'est pas un fichier PST Outlook (signature !BDN absente)"
    return None


def extractor_ready() -> str | None:
    """None si l'extracteur est utilisable, sinon le message d'erreur."""
    if not shutil.which("java"):
        return "Java introuvable (sudo apt install openjdk-21-jdk-headless)"
    if not JAR.is_file():
        return f"Bibliothèque absente : {JAR} (relancer install.sh)"
    if not all((CLASSES / "com" / "pff" / f"{c}.class").is_file() for c in ("PstExport", "PstIdentify")):
        return "Extracteur non compilé (relancer install.sh)"
    return None


def extract(pst_file: Path, out_dir: Path, tz: str, progress: Callable[[int], None],
            should_stop: Callable[[], bool]) -> Path:
    """Lance l'extracteur Java ; retourne le chemin de items.jsonl."""
    problem = check_pst_file(pst_file)
    if problem:
        raise ExtractError(f"{pst_file.name} : {problem}")
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["java", "-Xmx512m", "-cp", f"{JAR}:{CLASSES}", "com.pff.PstExport", str(pst_file), str(out_dir), tz],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
    )
    tail = []
    for line in proc.stderr:
        if should_stop():
            proc.kill()
            proc.wait()
            raise ExtractError("extraction interrompue")
        if line.startswith("PROGRESS "):
            progress(int(line.split()[1]))
        else:
            tail = (tail + [line.rstrip()])[-20:]
    if proc.wait() != 0:
        raise ExtractError(f"extracteur Java code {proc.returncode} : " + " | ".join(tail)[-800:])
    items = out_dir / "items.jsonl"
    if not items.is_file():
        raise ExtractError("extracteur : items.jsonl absent")
    return items


def read_items(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# --- Correspondance des dossiers ------------------------------------------------------

def view_of(container_class: str | None) -> str | None:
    """Vue Zimbra d'un dossier PST ; None = type non pris en charge (notes, journal)."""
    c = (container_class or "").upper()
    if c.startswith("IPF.APPOINTMENT"):
        return "appointment"
    if c.startswith("IPF.CONTACT"):
        return "contact"
    if c.startswith("IPF.TASK"):
        return "task"
    if c.startswith(("IPF.STICKYNOTE", "IPF.JOURNAL")):
        return None
    return "message"


class Target:
    """Destination Zimbra : dossier système (id) + segments à créer sous lui."""

    def __init__(self, base_id: str, segments: list[tuple[str, str]]):
        self.base_id = base_id
        self.segments = segments  # [(nom Zimbra, vue)]

    @property
    def key(self) -> str:
        """Chemin Zimbra prévu, stable entre simulation et import (clé d'idempotence)."""
        parts = [SYSTEM_PATHS[self.base_id]] + [n for n, _ in self.segments]
        return "/".join(p for p in parts if p)


class FolderMap:
    def __init__(self):
        self.folders: dict[tuple, dict] = {}  # chemin PST -> {class, view, excluded, target}

    def add(self, rec: dict) -> dict:
        path = tuple(rec["path"])
        cls = rec.get("class") or ""
        info = {"path": path, "class": cls, "count": rec.get("count", 0), "view": view_of(cls),
                "excluded": None, "target": None}
        rel = self.relative(path)
        parent = self.folders.get(path[:-1])
        if rec.get("ftype") == SEARCH_FOLDER_TYPE or (path and norm(path[0]).startswith("spam search folder")):
            info["excluded"] = "dossier de recherche"
        elif parent and parent["excluded"]:
            info["excluded"] = parent["excluded"]
        elif rel and (norm(rel[0]) in EXCLUDED_NAMES or (norm(rel[-1]) in EXCLUDED_ANY_LEVEL
                                                         and (info["view"] != "message" or len(rel) == 1))):
            info["excluded"] = "dossier technique Outlook"
        elif cls.upper().startswith(EXCLUDED_CLASS_PREFIXES):
            info["excluded"] = f"dossier technique ({cls})"
        elif info["view"] is None:
            info["excluded"] = f"type non pris en charge ({cls})"
        elif rel:
            info["target"] = self._target(rel, parent, info["view"])
        self.folders[path] = info
        return info

    @staticmethod
    def relative(path: tuple) -> tuple:
        """Retire le dossier racine « Top of Outlook data file » (ou équivalent)."""
        if path and norm(path[0]).startswith(TOP_PREFIXES):
            return path[1:]
        return path

    def _target(self, rel: tuple, parent: dict | None, view: str) -> Target:
        if len(rel) == 1:
            special = SPECIAL[view].get(norm(rel[0]))
            if special:
                return Target(special, [])
            return Target("1", [(zimbra_name(rel[0]), view)])
        if parent and parent["target"]:
            base = parent["target"]
            return Target(base.base_id, base.segments + [(zimbra_name(rel[-1]), view)])
        return Target("1", [(zimbra_name(rel[-1]), view)])

    def route(self, item: dict) -> tuple[Target | None, str | None]:
        """(destination, raison d'exclusion) pour un élément."""
        kind = item["kind"]
        f = self.folders.get(tuple(item.get("path") or ()))
        if f and f["excluded"]:
            return None, f["excluded"]
        if f and f["target"] and f["view"] == KIND_VIEW[kind]:
            return f["target"], None
        return Target(DEFAULT_FOLDER[kind], []), None


# --- Mails ---------------------------------------------------------------------------

MIME_POLICY = policy.default.clone(linesep="\r\n")
DROP_HEADERS = {"content-type", "content-transfer-encoding", "mime-version", "content-disposition"}


def mail_key(item: dict, email: str) -> str:
    mid = (item.get("msgid") or "").strip()
    return mid or f"<{item_uid(email, item['node'], item.get('src', ''))}@migration.invalid>"


def mail_date_ms(item: dict, sent: bool) -> int | None:
    order = ("submit_ms", "delivery_ms") if sent else ("delivery_ms", "submit_ms")
    return next((item[k] for k in order if item.get(k)), None)


def mail_flags(item: dict, target: Target) -> str:
    f = "" if item.get("read") else "u"
    f += "f" if item.get("flagged") else ""
    f += "r" if item.get("replied") else ""
    f += "w" if item.get("forwarded") else ""
    if target.base_id == "5" and not target.segments:
        f += "s"
    if target.base_id == "6" and not target.segments and item.get("unsent"):
        f += "d"
    return f


def _hdr(text: str) -> str:
    text = clean(text)
    return text if text.isascii() else Header(text, "utf-8").encode()


def _addr(name: str | None, smtp: str | None) -> str:
    name, smtp = clean(name), clean(smtp)
    if smtp:
        return formataddr((name, smtp), charset="utf-8")
    return _hdr(name) + " <>" if name else "<>"


def _raw_headers(raw: str) -> tuple[list[str], set[str]]:
    blocks: list[list[str]] = []
    for line in clean(raw).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        if line[0] in " \t" and blocks:
            blocks[-1].append(line)
        elif ":" in line:
            blocks.append([line])
    kept, names = [], set()
    for b in blocks:
        name = b[0].split(":", 1)[0].strip().lower()
        if name not in DROP_HEADERS:
            kept.append("\r\n".join(b))
            names.add(name)
    return kept, names


def _synth_headers(item: dict, email: str, names: set[str]) -> list[str]:
    h = []
    if "from" not in names:
        h.append("From: " + _addr(item.get("sender_name"), item.get("sender_smtp")))
    if not names & {"to", "cc"}:
        for rtype, label in ((1, "To"), (2, "Cc"), (3, "Bcc")):
            addrs = [_addr(r.get("name"), r.get("smtp")) for r in item.get("recipients", []) if r.get("type") == rtype]
            if addrs:
                h.append(f"{label}: " + ",\r\n ".join(addrs))
    if "subject" not in names:
        h.append("Subject: " + _hdr(item.get("subject") or ""))
    if "date" not in names:
        ms = item.get("submit_ms") or item.get("delivery_ms")
        if ms:
            h.append("Date: " + format_datetime(datetime.fromtimestamp(ms / 1000, timezone.utc)))
    if "message-id" not in names and (item.get("msgid") or "node" in item):  # pas pour les messages joints
        h.append("Message-ID: " + mail_key(item, email))
    if "in-reply-to" not in names and item.get("in_reply_to"):
        h.append("In-Reply-To: " + clean(item["in_reply_to"]))
    if "importance" not in names and item.get("importance") == 2:
        h.append("Importance: high")
    return h


def _body(item: dict, base: Path, email: str, warn: Callable[[str], None]) -> EmailMessage:
    msg = EmailMessage(policy=MIME_POLICY)
    text, html = clean(item.get("body")), clean(item.get("html"))
    if html and text:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text)
    html_part = msg.get_body(("html",)) if html else None

    # Deux passes : d'abord les images intégrées au corps HTML (multipart/related),
    # ensuite les pièces jointes (multipart/mixed). L'ordre inverse est refusé par la bibliothèque email.
    inline, others = [], []
    for att in item.get("attachments", []):
        cid = clean(att.get("cid")).strip("<>")
        is_inline = bool(cid and html_part is not None and att.get("file") and f"cid:{cid}" in html)
        (inline if is_inline else others).append(att)

    for att in inline + others:
        name = clean(att.get("name")) or "piece-jointe"
        if att.get("error"):
            warn(f"pièce jointe illisible : {att['error']}")
            continue
        if "message" in att:
            sub = BytesParser(policy=MIME_POLICY).parsebytes(build_mime(att["message"], base, email, warn))
            msg.add_attachment(sub, filename=name if name.endswith(".eml") else name + ".eml")
            continue
        if not att.get("file"):
            warn(f"pièce jointe sans contenu ignorée : {name} (méthode {att.get('method')})")
            continue
        data = (base / att["file"]).read_bytes()
        mime = (att.get("mime") or guess_type(name)[0] or "application/octet-stream").lower()
        maintype, _, subtype = mime.partition("/")
        if not subtype or maintype in ("multipart", "message"):
            maintype, subtype = "application", "octet-stream"
        if att in inline:
            cid = clean(att.get("cid")).strip("<>")
            html_part.add_related(data, maintype, subtype, cid=f"<{cid}>", filename=name)
        else:
            msg.add_attachment(data, maintype, subtype, filename=name)
    return msg


def build_mime(item: dict, base: Path, email: str, warn: Callable[[str], None] = lambda m: None) -> bytes:
    """Reconstruit le message RFC 822 : en-têtes d'origine si présents, sinon synthétisés."""
    kept, names = _raw_headers(item.get("headers") or "")
    headers = kept + _synth_headers(item, email, names)
    body = _body(item, base, email, warn).as_bytes()
    # Le corps commence par ses propres en-têtes MIME (MIME-Version, Content-Type...)
    return ("\r\n".join(headers) + "\r\n").encode("utf-8") + body


# --- iCalendar -----------------------------------------------------------------------

DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
F_DAILY, F_WEEKLY, F_MONTHLY, F_YEARLY = 0x200A, 0x200B, 0x200C, 0x200D
END_DATE, END_COUNT = 0x2021, 0x2022


def ical_text(s: str | None) -> str:
    return (clean(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", ""))


def fold(line: str) -> str:
    """Repli des lignes iCalendar/vCard à 75 octets."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 75:
            out.append(cur.decode("utf-8"))
            cur = b" "
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    m = abs(minutes)
    return f"{sign}{m // 60:02d}{m % 60:02d}"


def tzid_of(tz: dict) -> str:
    return re.sub(r'[;:,"]', " ", clean(tz.get("name")) or "PST").strip() or "PST"


def vtimezone(tz: dict) -> list[str]:
    std = -(tz.get("bias", 0) + tz.get("std_bias", 0))
    dst = -(tz.get("bias", 0) + tz.get("dst_bias", 0))
    lines = ["BEGIN:VTIMEZONE", f"TZID:{tzid_of(tz)}"]
    if not tz.get("std") or not tz.get("dst"):
        return lines + ["BEGIN:STANDARD", "DTSTART:16010101T000000", f"TZOFFSETFROM:{_offset(std)}",
                        f"TZOFFSETTO:{_offset(std)}", "END:STANDARD", "END:VTIMEZONE"]
    for comp, rule, frm, to in (("STANDARD", tz["std"], dst, std), ("DAYLIGHT", tz["dst"], std, dst)):
        week = -1 if rule["week"] >= 5 else rule["week"]
        lines += [f"BEGIN:{comp}", f"DTSTART:16010101T{rule['hour']:02d}{rule['minute']:02d}00",
                  f"RRULE:FREQ=YEARLY;BYMONTH={rule['month']};BYDAY={week}{DAYS[rule['dow'] % 7]}",
                  f"TZOFFSETFROM:{_offset(frm)}", f"TZOFFSETTO:{_offset(to)}", f"END:{comp}"]
    return lines + ["END:VTIMEZONE"]


def _days(mask: int) -> str:
    return ",".join(d for i, d in enumerate(DAYS) if mask & (1 << i))


def rrule(r: dict) -> str | None:
    """Motif de récurrence Outlook (MS-OXOCAL) -> RRULE. None si non convertible."""
    if r.get("calendar_type", 0) not in (0, 1):
        return None
    f, pt, per = r["freq"], r["ptype"], r.get("period", 1)
    spec, nth = r.get("spec", 0), r.get("nth", 0)
    setpos = -1 if nth >= 5 else nth
    if f == F_DAILY:
        parts = ["FREQ=DAILY", f"INTERVAL={max(1, per // 1440)}"] if pt == 0 else ["FREQ=WEEKLY", "BYDAY=MO,TU,WE,TH,FR"]
    elif f == F_WEEKLY:
        parts = ["FREQ=WEEKLY", f"INTERVAL={max(1, per)}", f"BYDAY={_days(spec) or 'MO'}"]
    elif f in (F_MONTHLY, F_YEARLY):
        if f == F_MONTHLY:
            parts = ["FREQ=MONTHLY", f"INTERVAL={max(1, per)}"]
        else:
            parts = ["FREQ=YEARLY", f"INTERVAL={max(1, per // 12)}", f"BYMONTH={int(r['start_local'][4:6])}"]
        if pt == 2:
            parts.append(f"BYMONTHDAY={-1 if spec >= 31 else spec}")
        elif pt == 3:
            parts += [f"BYDAY={_days(spec)}", f"BYSETPOS={setpos}"]
        elif pt == 4:
            parts.append("BYMONTHDAY=-1")
        else:
            return None
    else:
        return None
    firstdow = r.get("firstdow")
    if f == F_WEEKLY and isinstance(firstdow, int) and 0 <= firstdow <= 6:
        parts.append(f"WKST={DAYS[firstdow]}")
    if r.get("end_type") == END_COUNT and r.get("count"):
        parts.append(f"COUNT={r['count']}")
    elif r.get("end_type") == END_DATE and r.get("end_date"):
        parts.append(f"UNTIL={r['end_date']}T235959Z")
    return "RRULE:" + ";".join(parts)


def vevent(item: dict, email: str, warn: Callable[[str], None] = lambda m: None) -> tuple[list[str], list[str]]:
    """(lignes VEVENT, lignes VTIMEZONE nécessaires) pour un rendez-vous."""
    uid = item_uid(email, item["node"], item.get("src", ""))
    common = [f"UID:{uid}", f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
              f"SUMMARY:{ical_text(item.get('subject'))}"]
    if item.get("location"):
        common.append(f"LOCATION:{ical_text(item['location'])}")
    if item.get("body"):
        common.append(f"DESCRIPTION:{ical_text(item['body'])}")
    busy = item.get("busy", 2)
    common.append("TRANSP:TRANSPARENT" if busy == 0 else "TRANSP:OPAQUE")
    common.append("X-MICROSOFT-CDO-BUSYSTATUS:" + {0: "FREE", 1: "TENTATIVE", 3: "OOF"}.get(busy, "BUSY"))
    if item.get("sensitivity") == 2:
        common.append("CLASS:PRIVATE")
    attendees = [a for a in item.get("attendees", []) if a.get("smtp")]
    if attendees:
        if item.get("organizer_smtp"):
            common.append(f'ORGANIZER;CN="{ical_text(item.get("organizer_name"))}":mailto:{item["organizer_smtp"]}')
        for a in attendees:
            role = "OPT-PARTICIPANT" if a.get("type") == 2 else "REQ-PARTICIPANT"
            cutype = ";CUTYPE=RESOURCE" if a.get("type") == 3 else ""
            common.append(f'ATTENDEE;CN="{ical_text(a.get("name"))}";ROLE={role}{cutype}:mailto:{a["smtp"]}')
    alarm = []
    if item.get("reminder_set") and item.get("reminder_min") is not None:
        alarm = ["BEGIN:VALARM", f"TRIGGER:-PT{max(0, item['reminder_min'])}M", "ACTION:DISPLAY",
                 "DESCRIPTION:Rappel", "END:VALARM"]

    rec, tzlines = item.get("rec"), []
    rule = rrule(rec) if rec else None
    if rec and not rule:
        warn(f"récurrence non convertible ({rec.get('freq')}/{rec.get('ptype')}) : importé comme rendez-vous unique")
    if item.get("rec_error"):
        warn(f"récurrence illisible : {item['rec_error']} — importé comme rendez-vous unique")

    allday = bool(item.get("allday"))
    if rule:
        tzid = tzid_of(rec["tz"])
        if allday:
            start = f"DTSTART;VALUE=DATE:{rec['start_local'][:8]}"
            end_d = item.get("end_date") or rec["end_local"][:8]
            end = f"DTEND;VALUE=DATE:{end_d}"
            exd = [f"EXDATE;VALUE=DATE:{d}" for d in rec.get("exdates", [])]
        else:
            tzlines = vtimezone(rec["tz"])
            start = f"DTSTART;TZID={tzid}:{rec['start_local']}"
            end = f"DTEND;TZID={tzid}:{rec['end_local']}"
            exd = [f"EXDATE;TZID={tzid}:{d}{rec['start_local'][8:]}" for d in rec.get("exdates", [])]
        lines = ["BEGIN:VEVENT", *common, start, end, rule, *exd, *alarm, "END:VEVENT"]
        for e in rec.get("exceptions", []):
            if not e.get("orig_local") or not e.get("start_local"):
                continue
            ex = [l for l in common if not l.startswith(("SUMMARY:", "LOCATION:"))]
            ex.append(f"SUMMARY:{ical_text(e.get('subject') or item.get('subject'))}")
            loc = e.get("location") or item.get("location")
            if loc:
                ex.append(f"LOCATION:{ical_text(loc)}")
            if allday:
                ex += [f"RECURRENCE-ID;VALUE=DATE:{e['orig_local'][:8]}",
                       f"DTSTART;VALUE=DATE:{e['start_local'][:8]}", f"DTEND;VALUE=DATE:{e['end_local'][:8]}"]
            else:
                ex += [f"RECURRENCE-ID;TZID={tzid}:{e['orig_local']}",
                       f"DTSTART;TZID={tzid}:{e['start_local']}", f"DTEND;TZID={tzid}:{e['end_local']}"]
            lines += ["BEGIN:VEVENT", *ex, "END:VEVENT"]
        return lines, tzlines

    if allday and item.get("start_date"):
        end_d = item.get("end_date") or item["start_date"]
        if end_d <= item["start_date"]:
            end_d = (datetime.strptime(item["start_date"], "%Y%m%d").toordinal() + 1)
            end_d = datetime.fromordinal(end_d).strftime("%Y%m%d")
        times = [f"DTSTART;VALUE=DATE:{item['start_date']}", f"DTEND;VALUE=DATE:{end_d}"]
    elif item.get("start_ms"):
        times = [f"DTSTART:{_utc(item['start_ms'])}", f"DTEND:{_utc(item.get('end_ms') or item['start_ms'])}"]
    else:
        raise ValueError("rendez-vous sans date de début")
    return ["BEGIN:VEVENT", *common, *times, *alarm, "END:VEVENT"], tzlines


def vcalendar(components: list[list[str]], timezones: list[list[str]]) -> bytes:
    seen, tzs = set(), []
    for tz in timezones:
        if tz and tz[1] not in seen:
            seen.add(tz[1])
            tzs += tz
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Migration PST Zimbra//FR", *tzs]
    for c in components:
        lines += c
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8")


TASK_STATUS = {0: "NEEDS-ACTION", 1: "IN-PROCESS", 2: "COMPLETED", 3: "NEEDS-ACTION", 4: "CANCELLED"}


def vtodo(item: dict, email: str) -> list[str]:
    lines = ["BEGIN:VTODO", f"UID:{item_uid(email, item['node'], item.get('src', ''))}",
             f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}", f"SUMMARY:{ical_text(item.get('subject'))}"]
    if item.get("body"):
        lines.append(f"DESCRIPTION:{ical_text(item['body'])}")
    start, due = item.get("start"), item.get("due")
    if start and (not due or start <= due):
        lines.append(f"DTSTART;VALUE=DATE:{start}")
    if due:
        lines.append(f"DUE;VALUE=DATE:{due}")
    status = "COMPLETED" if item.get("complete") else TASK_STATUS.get(item.get("status"), "NEEDS-ACTION")
    lines.append(f"STATUS:{status}")
    lines.append(f"PERCENT-COMPLETE:{100 if status == 'COMPLETED' else int(round((item.get('percent') or 0) * 100))}")
    if status == "COMPLETED" and item.get("completed_ms"):
        lines.append(f"COMPLETED:{_utc(item['completed_ms'])}")
    lines.append(f"PRIORITY:{ {2: 1, 0: 9}.get(item.get('importance'), 5) }")
    return lines + ["END:VTODO"]


# --- vCard ---------------------------------------------------------------------------

def vcard_text(s: str | None) -> str:
    return (clean(s).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", ""))


def vcard(item: dict, email: str) -> bytes:
    g = lambda k: item.get(k) or ""  # noqa: E731
    fn = g("display") or " ".join(p for p in (g("given"), g("surname")) if p) or g("company") or g("email1") or "Sans nom"
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{item_uid(email, item['node'], item.get('src', ''))}", f"FN:{vcard_text(fn)}",
             "N:" + ";".join(vcard_text(g(k)) for k in ("surname", "given", "middle", "prefix", "suffix"))]
    if g("nickname"):
        lines.append(f"NICKNAME:{vcard_text(g('nickname'))}")
    if g("company") or g("department"):
        lines.append(f"ORG:{vcard_text(g('company'))};{vcard_text(g('department'))}")
    if g("title"):
        lines.append(f"TITLE:{vcard_text(g('title'))}")
    for k in ("email1", "email2", "email3"):
        if g(k):
            lines.append(f"EMAIL;TYPE=INTERNET:{vcard_text(g(k))}")
    for k, t in (("tel_work", "WORK,VOICE"), ("tel_work2", "WORK,VOICE"), ("tel_home", "HOME,VOICE"),
                 ("tel_home2", "HOME,VOICE"), ("tel_mobile", "CELL"), ("tel_other", "VOICE"),
                 ("tel_company", "WORK,PREF"), ("tel_assistant", "WORK"), ("tel_pager", "PAGER"),
                 ("fax_work", "WORK,FAX"), ("fax_home", "HOME,FAX")):
        if g(k):
            lines.append(f"TEL;TYPE={t}:{vcard_text(g(k))}")
    for prefix, t in (("work", "WORK"), ("home", "HOME"), ("other", "OTHER")):
        parts = [g(f"{prefix}_{k}") for k in ("street", "city", "state", "postal", "country")]
        if any(parts):
            lines.append(f"ADR;TYPE={t}:;;" + ";".join(vcard_text(p) for p in parts))
    for k, t in (("url_work", "WORK"), ("url_home", "HOME")):
        if g(k):
            lines.append(f"URL;TYPE={t}:{vcard_text(g(k))}")
    if g("birthday"):
        lines.append(f"BDAY:{g('birthday')}")
    if g("note"):
        lines.append(f"NOTE:{vcard_text(g('note'))}")
    lines.append("END:VCARD")
    return ("\r\n".join(fold(l) for l in lines) + "\r\n").encode("utf-8")
