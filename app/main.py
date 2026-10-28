"""Plateforme de migration PST -> Zimbra : interface web."""
import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import access, brand, checks, config, db, folders, inventory, mailbox, migration, pst, state, views
from .zimbra import ZimbraError, get_info

LOG_PATH = db.STATE_DIR / "logs" / "app.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

EMAIL_RE = inventory.EMAIL_RE
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Accès réseau (VM cliente) : noms / adresses IP autorisés, séparés par des virgules (fichier migration.env)
EXTRA_HOSTS = {h.strip().lower().strip("[]") for h in os.environ.get("MIGRATION_ALLOWED_HOSTS", "").split(",") if h.strip()}
ALLOWED_HOSTS = LOCAL_HOSTS | EXTRA_HOSTS
PUBLIC_PATHS = ("/connexion", "/static/")
CONFIRM_WORD = "IMPORTER"

db.init()
pst.WORK_DIR.mkdir(exist_ok=True)
migration.recover_on_startup()
app = FastAPI(title="Migration PST → Zimbra", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=db.BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=db.BASE_DIR / "app" / "templates")
templates.env.filters["dt"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else ""
templates.env.filters["mo"] = lambda n: f"{n / 1048576:,.1f} Mo".replace(",", " ")
templates.env.filters["pct"] = lambda x: f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")
templates.env.filters["n"] = lambda x: f"{x:,}".replace(",", " ")


def _taille(n: int) -> str:
    for unit, div in (("To", 1 << 40), ("Go", 1 << 30), ("Mo", 1 << 20)):
        if n >= div:
            return f"{n / div:,.1f} {unit}".replace(",", " ").replace(".", ",")
    return f"{n / 1024:.0f} Ko"


templates.env.filters["taille"] = _taille

NAV = [
    ("Pilotage", [("/", "Tableau de bord", "dashboard"), ("/suivi", "Suivi des lots", "activity")]),
    ("Préparation", [("/connectivite", "Connectivité", "plug"), ("/configuration", "Configuration", "settings"),
                     ("/inventaire", "Inventaire", "archive"), ("/jeton", "Test jeton", "key")]),
    ("Migration", [("/migration", "Nouvelle migration", "send")]),
    ("Aide", [("/fonctionnement", "Fonctionnement", "book")]),
]


def _host_of(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:value.index("]")] if "]" in value else value
    return value.rsplit(":", 1)[0]


@app.middleware("http")
async def guard(request: Request, call_next):
    """Hôtes autorisés (DNS rebinding), origine des POST (CSRF), mot de passe d'accès si ouvert au réseau."""
    if _host_of(request.headers.get("host", "")) not in ALLOWED_HOSTS:
        return PlainTextResponse("Hôte non autorisé (ajouter ce nom ou cette IP à MIGRATION_ALLOWED_HOSTS "
                                 "dans migration.env)", status_code=403)
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "").lower() not in ALLOWED_HOSTS:
            return PlainTextResponse("Origine non autorisée", status_code=403)
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    if EXTRA_HOSTS and not access.enabled():
        return PlainTextResponse("Interface ouverte sur le réseau sans mot de passe d'accès : "
                                 "lancez ./set-password.sh sur le serveur.", status_code=503)
    if access.enabled() and not access.valid(request.cookies.get(access.COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"error": "session expirée"}, status_code=401)
        return RedirectResponse(f"/connexion?next={quote(path)}", status_code=303)
    return await call_next(request)


def render(request: Request, name: str, **ctx):
    ctx.setdefault("settings", config.get_settings())
    ctx["password_set"] = config.admin_password.get() is not None
    ctx["nav"] = NAV
    ctx["path"] = request.url.path
    ctx["brand"] = brand.context()
    ctx["auth"] = access.enabled()
    # version de la feuille de style : le navigateur recharge le CSS dès qu'il change (pas de cache périmé)
    ctx["css_v"] = int((db.BASE_DIR / "app" / "static" / "style.css").stat().st_mtime)
    return templates.TemplateResponse(request, name, ctx)


# --- Connexion à l'interface --------------------------------------------------

@app.get("/connexion")
def login_page(request: Request, next: str = "/", error: str | None = None):
    return templates.TemplateResponse(request, "connexion.html", {"brand": brand.context(), "next": next,
                                      "error": error, "css_v": int((db.BASE_DIR / "app" / "static" / "style.css").stat().st_mtime),
                                      "enabled": access.enabled()})


@app.post("/connexion")
async def login(request: Request, password: str = Form(""), next: str = Form("/")):
    if not access.enabled() or not await run_in_threadpool(access.check_password, password):
        log.warning("Échec de connexion à l'interface depuis %s", request.client.host if request.client else "?")
        await asyncio.sleep(1.5)  # freine les essais de mot de passe
        return RedirectResponse(f"/connexion?error=1&next={quote(next)}", status_code=303)
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(access.COOKIE, access.open_session(), httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=access.SESSION_TTL_S)
    log.info("Connexion à l'interface depuis %s", request.client.host if request.client else "?")
    return resp


@app.post("/deconnexion")
def logout(request: Request):
    access.close_session(request.cookies.get(access.COOKIE))
    resp = RedirectResponse("/connexion", status_code=303)
    resp.delete_cookie(access.COOKIE)
    return resp


# --- Choix du dossier PST -------------------------------------------------------

@app.get("/api/dossiers")
def folders_api(path: str = ""):
    try:
        data = folders.listing(path) if path else {"suggestions": folders.suggestions()}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/")
def index(request: Request, error: str | None = None):
    # Première visite avec les identifiants admin : synchronisation automatique de l'annuaire
    if config.admin_password.get() and not inventory.directory_synced_at():
        inventory.start_job()
    elif config.admin_password.get():
        mailbox.refresh_usage_if_stale()  # taille réelle des boîtes, rafraîchie à chaque visite (1 appel admin)
    return render(request, "dashboard.html", d=views.dashboard(), categories=views.CATEGORIES, error=error)


@app.get("/fonctionnement")
def how_it_works(request: Request):
    return render(request, "fonctionnement.html")


# --- Configuration -----------------------------------------------------------

@app.get("/configuration")
def config_page(request: Request):
    return render(request, "configuration.html", errors=[], saved=False)


@app.post("/configuration")
def config_save(
    request: Request,
    admin_url: str = Form(...),
    user_url: str = Form(...),
    admin_user: str = Form(""),
    admin_password: str = Form(""),
    pst_dir: str = Form(...),
    parallelism: str = Form("3"),
    ca_file: str = Form(""),
    timezone: str = Form("Africa/Tunis"),
    domain: str = Form(""),
    connections: str = Form("4"),
):
    form = dict(admin_url=admin_url, user_url=user_url, admin_user=admin_user, pst_dir=pst_dir,
                parallelism=parallelism, ca_file=ca_file, timezone=timezone, domain=domain, connections=connections)
    values, errors = config.validate(form)
    if errors:
        return render(request, "configuration.html", errors=errors, saved=False, form=values)
    config.save(values)
    if admin_password:
        config.admin_password.set(admin_password)
    state.set_admin_session(None)
    mailbox.drop_sessions()
    log.info("Configuration enregistrée (admin=%s, mot de passe %s)", values["admin_user"],
             "saisi" if admin_password else "inchangé")
    if admin_password and values["admin_user"]:
        # Identifiants admin saisis : annuaire + analyse des PST lancés automatiquement
        inventory.start_job()
        return RedirectResponse("/", status_code=303)
    return render(request, "configuration.html", errors=[], saved=True)


@app.post("/configuration/oublier")
def forget_password():
    config.admin_password.set(None)
    state.set_admin_session(None)
    log.info("Mot de passe admin effacé de la mémoire")
    return RedirectResponse("/configuration", status_code=303)


# --- Connectivité ------------------------------------------------------------

@app.get("/connectivite")
def connectivity_page(request: Request):
    return render(request, "connectivite.html", results=None)


@app.post("/connectivite")
def connectivity_run(request: Request):
    s = config.get_settings()
    results, session = checks.run_all(s, config.admin_password.get())
    if session is not None:
        state.set_admin_session(session)
    for r in results:
        log.info("Connectivité %s : %s %s", r["label"], r["status"], r["detail"])
    return render(request, "connectivite.html", results=results)


# --- Inventaire --------------------------------------------------------------

@app.get("/inventaire")
def inventory_page(request: Request, error: str | None = None, msg: str | None = None):
    files = inventory.files()
    return render(request, "inventaire.html", files=files, job=inventory.job_state(), error=error, msg=msg,
                  accounts=sorted(inventory.directory()), synced_at=inventory.directory_synced_at())


@app.post("/inventaire")
def inventory_scan(next: str = Form("/inventaire")):
    back = next if next in ("/", "/inventaire") else "/inventaire"
    try:
        state.admin_session()
    except ZimbraError as e:
        return RedirectResponse(f"/inventaire?error={quote(str(e))}", status_code=303)
    inventory.start_job()
    return RedirectResponse(back, status_code=303)


@app.post("/inventaire/valider")
def inventory_validate(file: str = Form(...), email: str = Form(...)):
    try:
        inventory.validate(file, email)
    except RuntimeError as e:
        return RedirectResponse(f"/inventaire?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/inventaire?msg={quote('Attribution validée : ' + email)}", status_code=303)


@app.post("/inventaire/valider-tout")
def inventory_validate_all():
    n = inventory.validate_all_proposed()
    return RedirectResponse(f"/inventaire?msg={quote(f'{n} attribution(s) validée(s)')}", status_code=303)


@app.post("/inventaire/annuler")
def inventory_unvalidate(file: str = Form(...)):
    inventory.unvalidate(file)
    return RedirectResponse("/inventaire", status_code=303)


@app.get("/api/inventaire")
def inventory_api():
    return JSONResponse(inventory.job_state(), headers={"Cache-Control": "no-store"})


# --- Boîtes Zimbra (état réel, lecture seule) ----------------------------------

@app.get("/comptes/{email}")
def account_page(request: Request, email: str, msg: str | None = None, error: str | None = None):
    email = email.lower()
    acc = inventory.directory().get(email)
    if acc is None:
        return RedirectResponse("/", status_code=303)
    progress = next((r for r in views.accounts_progress()["rows"] if r["email"] == email), None)
    runs = db.query("""SELECT a.*, r.mode, r.created FROM run_accounts a JOIN runs r ON r.id = a.run_id
                       WHERE a.email = ? ORDER BY a.run_id DESC""", (email,))
    files = [f for f in inventory.files() if f["email"] == email]
    return render(request, "compte.html", acc=acc, p=progress, runs=runs, files=files, usage=mailbox.usage().get(email),
                  check=mailbox.last_check(email), msg=msg, error=error, categories=views.CAT,
                  active=email in migration.active_emails())


@app.get("/api/comptes/{email}/etat")
def account_state_api(email: str):
    email = email.lower()
    if email not in inventory.directory():
        return JSONResponse({"error": "compte inconnu"}, status_code=404)
    try:
        data = mailbox.live_state(email)
    except ZimbraError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    data["active"] = email in migration.active_emails()
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.post("/comptes/{email}/verifier")
def account_resync(email: str):
    email = email.lower()
    if email in migration.active_emails():
        return RedirectResponse(f"/comptes/{quote(email)}?error={quote('Import en cours pour ce compte')}", status_code=303)
    try:
        r = mailbox.resync(email)
    except ZimbraError as e:
        return RedirectResponse(f"/comptes/{quote(email)}?error={quote(str(e))}", status_code=303)
    m = (f"{r['removed']} élément(s) importé(s) ne sont plus dans la boîte : ils seront réimportés au prochain lot"
         if r["removed"] else "La boîte contient bien tout ce qui a été importé")
    return RedirectResponse(f"/comptes/{quote(email)}?msg={quote(m)}", status_code=303)


@app.post("/comptes/{email}/reinitialiser")
def account_reset(email: str, confirm: str = Form("")):
    email = email.lower()
    if confirm.strip() != "REINITIALISER":
        return RedirectResponse(f"/comptes/{quote(email)}?error={quote('Saisir REINITIALISER pour confirmer')}", status_code=303)
    if email in migration.active_emails():
        return RedirectResponse(f"/comptes/{quote(email)}?error={quote('Import en cours pour ce compte')}", status_code=303)
    n = mailbox.reset_history(email)
    return RedirectResponse(f"/comptes/{quote(email)}?msg={quote(f'Historique effacé ({n} éléments) : le prochain import repartira de zéro')}", status_code=303)


@app.post("/boites/verifier")
def verify_all():
    try:
        state.admin_session()
    except ZimbraError as e:
        return RedirectResponse(f"/?error={quote(str(e))}", status_code=303)
    mailbox.start_verify_all()
    return RedirectResponse("/", status_code=303)


@app.get("/api/tableau-de-bord")
def dashboard_api():
    if config.admin_password.get():
        mailbox.refresh_usage_if_stale()
    d = views.accounts_progress()
    d["rows"] = {r["email"]: r["cat"] for r in d["rows"]}
    return JSONResponse(d, headers={"Cache-Control": "no-store"})


# --- Test jeton (lecture seule) ----------------------------------------------

@app.get("/jeton")
def token_page(request: Request):
    return render(request, "jeton.html", email="", steps=None)


@app.post("/jeton")
def token_run(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    steps = []
    if not EMAIL_RE.match(email):
        steps.append({"label": "Adresse", "status": "KO", "detail": "Adresse email invalide"})
        return render(request, "jeton.html", email=email, steps=steps, result=None)

    s = config.get_settings()
    try:
        session = state.admin_session()
        token, lifetime_ms = session.delegate_auth(email)
        steps.append({"label": "DelegateAuthRequest", "status": "OK",
                      "detail": f"Jeton délégué obtenu (durée {lifetime_ms // 1000} s, non affiché)"})
    except ZimbraError as e:
        steps.append({"label": "DelegateAuthRequest", "status": "KO", "detail": str(e)})
        log.info("Test jeton %s : DelegateAuth KO %s", email, e)
        return render(request, "jeton.html", email=email, steps=steps, result=None)

    try:
        result = get_info(s, token)
    except ZimbraError as e:
        steps.append({"label": f"GetInfoRequest ({s.user_soap_url})", "status": "KO", "detail": str(e)})
        log.info("Test jeton %s : GetInfo KO %s", email, e)
        return render(request, "jeton.html", email=email, steps=steps, result=None)

    returned = (result.get("name") or "").lower()
    match = returned == email
    steps.append({"label": f"GetInfoRequest ({s.user_soap_url})", "status": "OK",
                  "detail": f"Compte retourné : {result.get('name')}"})
    steps.append({"label": "Concordance", "status": "OK" if match else "KO",
                  "detail": "Le compte du jeton correspond à l'adresse demandée" if match
                  else f"Différence : demandé {email}, retourné {result.get('name')}"})
    log.info("Test jeton %s : GetInfo name=%s concordance=%s", email, result.get("name"), match)
    return render(request, "jeton.html", email=email, steps=steps, result=result)


# --- Migration ---------------------------------------------------------------

def migration_page_ctx(error: str | None = None):
    progress = {r["email"]: r for r in views.accounts_progress()["rows"]}
    rows = [{"email": e, "files": [Path(f["file"]).name for f in fs], "size": sum(f["size"] for f in fs),
             "progress": progress.get(e)} for e, fs in inventory.ready_accounts().items()]
    return dict(rows=rows, busy=migration.active_emails(), extractor_error=pst.extractor_ready(),
                confirm_word=CONFIRM_WORD, error=error)


@app.get("/migration")
def migration_page(request: Request, email: str | None = None):
    return render(request, "migration.html", preselect=(email or "").lower(), **migration_page_ctx())


@app.post("/migration")
async def migration_start(request: Request):
    form = await request.form()
    emails = sorted({e.lower() for e in form.getlist("emails")})
    mode = {"simulation": migration.SIMULATION, "reel": migration.REAL}.get(form.get("mode", "simulation"))
    since = (form.get("since") or "").strip() or None
    confirm = (form.get("confirm") or "").strip()

    def fail(msg: str):
        return render(request, "migration.html", **migration_page_ctx(error=msg))

    if not emails:
        return fail("Aucun compte sélectionné.")
    if mode is None:
        return fail("Mode inconnu.")
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        return fail("Date du mode delta invalide (AAAA-MM-JJ).")
    if mode == migration.REAL and (confirm != CONFIRM_WORD or form.get("confirm_check") != "1"):
        return fail(f"Import réel non confirmé : cochez la case et saisissez {CONFIRM_WORD}.")
    try:
        state.admin_session()
        run_id = await run_in_threadpool(migration.start, emails, mode, since)
    except (ZimbraError, RuntimeError) as e:
        return fail(str(e))
    log.info("Lot %d demandé : %s, %d comptes, depuis %s", run_id, mode, len(emails), since or "-")
    return RedirectResponse(f"/suivi/{run_id}", status_code=303)


# --- Suivi -------------------------------------------------------------------

@app.get("/suivi")
def runs_page(request: Request):
    runs = db.query("""SELECT r.*, COUNT(a.email) AS n,
                              SUM(a.status IN ('terminé', 'simulation OK')) AS ok,
                              SUM(a.status IN ('bloqué', 'erreur', 'refusé', 'terminé avec erreurs')) AS ko
                       FROM runs r LEFT JOIN run_accounts a ON a.run_id = r.id
                       GROUP BY r.id ORDER BY r.id DESC""")
    return render(request, "suivi.html", runs=runs)


@app.get("/suivi/{run_id}")
def run_page(request: Request, run_id: int, error: str | None = None):
    view = views.run_view(run_id)
    if view is None:
        return RedirectResponse("/suivi", status_code=303)
    return render(request, "lot.html", view=view, error=error)


@app.get("/api/lots/{run_id}")
def run_api(run_id: int):
    view = views.run_view(run_id)
    if view is None:
        return JSONResponse({"error": "lot inconnu"}, status_code=404)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


@app.post("/suivi/{run_id}/arreter")
def run_stop(run_id: int):
    migration.stop(run_id)
    log.info("Arrêt demandé pour le lot %d", run_id)
    return RedirectResponse(f"/suivi/{run_id}", status_code=303)


@app.post("/suivi/{run_id}/reprendre")
def run_resume(request: Request, run_id: int):
    try:
        state.admin_session()
        migration.resume(run_id)
    except (ZimbraError, RuntimeError) as e:
        return run_page(request, run_id, error=str(e))
    log.info("Reprise du lot %d", run_id)
    return RedirectResponse(f"/suivi/{run_id}", status_code=303)


@app.get("/suivi/{run_id}/rapport.csv")
def run_csv(run_id: int):
    if not db.query("SELECT 1 FROM runs WHERE id = ?", (run_id,)):
        return PlainTextResponse("Lot inconnu", status_code=404)
    return Response(migration.report_csv(run_id), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="rapport-lot-{run_id}.csv"'})


@app.get("/suivi/{run_id}/log/{email}")
def run_log(run_id: int, email: str):
    if not db.query("SELECT 1 FROM run_accounts WHERE run_id = ? AND email = ?", (run_id, email)):
        return PlainTextResponse("Compte inconnu dans ce lot", status_code=404)
    p = migration.log_path(run_id, email)
    return PlainTextResponse(p.read_text(encoding="utf-8") if p.is_file() else "(journal vide)")
