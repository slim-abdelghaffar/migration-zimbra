"""Identité visuelle NextStep : modifier ici le nom, les couleurs ou le logo.

Logo officiel : déposer app/static/brand/logo.svg (ou logo.png) ; il remplace le monogramme par défaut.
"""
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"

COMPANY = "NextStep"
PRODUCT = "Migration Suite"
TAGLINE = "Migration PST → Zimbra"
VERSION = "1.0"

COLORS = {
    "brand": "#0b2e59",     # bleu nuit (barre latérale, boutons)
    "brand-2": "#164b8f",
    "accent": "#00a0df",    # cyan (actions, progression)
    "accent-soft": "#e0f4fc",
}


def logo_url() -> str | None:
    for name in ("logo.svg", "logo.png"):
        if (STATIC / "brand" / name).is_file():
            return f"/static/brand/{name}"
    return None


def context() -> dict:
    return {"company": COMPANY, "product": PRODUCT, "tagline": TAGLINE, "version": VERSION,
            "colors": COLORS, "logo": logo_url()}
