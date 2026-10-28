"""Choix du dossier PST : volumes partagés montés et navigation dans les dossiers (lecture seule)."""
import os
import time
from pathlib import Path

SHARED_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "9p", "vboxsf", "virtiofs", "drvfs",
             "fuse.sshfs", "fuse.vmhgfs-fuse", "fuse.rclone", "ntfs", "ntfs3", "fuseblk"}
SCAN_LIMIT = 20000      # entrées parcourues au maximum pour compter les PST d'une arborescence
SCAN_TIMEOUT_S = 3


def mounts() -> list[dict]:
    """Points de montage, en signalant les volumes partagés (SMB/CIFS, NFS, dossiers partagés de VM)."""
    out = []
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        src, mnt, fstype, opts = parts[0], parts[1].replace("\\040", " "), parts[2], parts[3].split(",")
        if fstype in SHARED_FS and not mnt.startswith(("/proc", "/sys")):
            out.append({"path": mnt, "source": src, "fstype": fstype, "readonly": "ro" in opts})
    return out


def mount_of(path: Path) -> dict | None:
    best = None
    for m in mounts():
        if str(path) == m["path"] or str(path).startswith(m["path"].rstrip("/") + "/"):
            if best is None or len(m["path"]) > len(best["path"]):
                best = m
    return best


def count_pst(root: Path) -> tuple[int, int, bool]:
    """(nombre de PST, taille totale, comptage complet ?) dans l'arborescence, avec garde-fous de durée."""
    n = size = seen = 0
    deadline = time.time() + SCAN_TIMEOUT_S
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > SCAN_LIMIT or time.time() > deadline:
                        return n, size, False
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith("."):
                                stack.append(Path(e.path))
                        elif e.name.lower().endswith(".pst"):
                            n += 1
                            size += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return n, size, True


def listing(path: str) -> dict:
    p = Path(path or "/").expanduser()
    if not p.is_absolute():
        raise ValueError("chemin absolu attendu")
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"dossier introuvable : {p}")
    subdirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            try:
                if e.is_dir() and not e.name.startswith("."):
                    subdirs.append(e.name)
            except OSError:
                continue
    except PermissionError:
        raise ValueError(f"accès refusé : {p}") from None
    n, size, complete = count_pst(p)
    return {
        "path": str(p), "parent": str(p.parent) if p != p.parent else None,
        "subdirs": subdirs[:500], "pst": n, "size": size, "complete": complete,
        "writable": os.access(p, os.W_OK), "mount": mount_of(p),
    }


def suggestions() -> list[dict]:
    """Emplacements proposés : volumes partagés montés d'abord, puis dossiers usuels."""
    seen, out = set(), []
    for m in mounts():
        if m["path"] not in seen:
            seen.add(m["path"])
            out.append({"path": m["path"], "label": f"{m['fstype'].upper()} · {m['source']}", "shared": True,
                        "readonly": m["readonly"]})
    for p in ("/mnt", "/media", "/srv", str(Path.home())):
        if Path(p).is_dir() and p not in seen:
            seen.add(p)
            out.append({"path": p, "label": "dossier local", "shared": False, "readonly": False})
    return out
