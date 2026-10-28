"""Client SOAP Zimbra (XML SOAP 1.2).

- Les jetons et le mot de passe ne sont jamais journalisés ni renvoyés à l'interface.
- La vérification TLS est toujours active (magasin système + CA optionnelle).
"""
import logging
import ssl
import threading
import time
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx

from .config import Settings

log = logging.getLogger("zimbra")

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
NS_ZIMBRA = "urn:zimbra"
NS_ADMIN = "urn:zimbraAdmin"
NS_ACCOUNT = "urn:zimbraAccount"
NS_MAIL = "urn:zimbraMail"

ET.register_namespace("soap", SOAP_NS)

AUTH_EXPIRED_CODES = {"service.AUTH_EXPIRED", "service.AUTH_REQUIRED"}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ZimbraError(Exception):
    def __init__(self, message: str, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status

    def __str__(self) -> str:
        parts = []
        if self.http_status:
            parts.append(f"HTTP {self.http_status}")
        if self.code:
            parts.append(self.code)
        return (" / ".join(parts) + " : " if parts else "") + self.message


def ssl_context(ca_file: str = "") -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_file:
        ctx.load_verify_locations(ca_file)
    return ctx


def _el(ns: str, local: str, parent: ET.Element | None = None, text: str | None = None, /, **attrs) -> ET.Element:
    """Élément XML ; les attributs (dont « name ») passent en mots-clés."""
    tag = f"{{{ns}}}{local}"
    e = ET.SubElement(parent, tag, attrs) if parent is not None else ET.Element(tag, attrs)
    if text is not None:
        e.text = text
    return e


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return c.text
    return None


def _envelope(body_el: ET.Element, auth_token: str | None) -> bytes:
    env = _el(SOAP_NS, "Envelope")
    header = _el(SOAP_NS, "Header", env)
    ctx = _el(NS_ZIMBRA, "context", header)
    _el(NS_ZIMBRA, "nosession", ctx)
    if auth_token:
        _el(NS_ZIMBRA, "authToken", ctx, auth_token)
    _el(SOAP_NS, "Body", env).append(body_el)
    return ET.tostring(env, encoding="utf-8", xml_declaration=True)


def http_client(ca_file: str) -> httpx.Client:
    """Client HTTP réutilisable (connexions persistantes), vérification TLS active."""
    return httpx.Client(verify=ssl_context(ca_file), timeout=TIMEOUT)


def soap_call(url: str, body_el: ET.Element, ca_file: str, auth_token: str | None = None,
              client: httpx.Client | None = None) -> ET.Element:
    """Envoie une requête SOAP et retourne le premier élément du Body (ou lève ZimbraError)."""
    op = _local(body_el.tag)
    try:
        kwargs = dict(content=_envelope(body_el, auth_token),
                      headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        if client is not None:
            r = client.post(url, **kwargs)
        else:
            r = httpx.post(url, verify=ssl_context(ca_file), timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as e:
        raise ZimbraError(f"{op} : {type(e).__name__} : {e}") from None

    try:
        root = ET.fromstring(r.content)
        body = root.find(f"{{{SOAP_NS}}}Body")
        if body is None or len(body) == 0:
            raise ET.ParseError("Body SOAP absent")
    except ET.ParseError:
        snippet = r.text[:300].replace("\n", " ")
        raise ZimbraError(f"{op} : réponse non SOAP : {snippet}", http_status=r.status_code) from None

    fault = body.find(f"{{{SOAP_NS}}}Fault")
    if fault is not None:
        reason = fault.findtext(f".//{{{SOAP_NS}}}Text") or "Fault SOAP"
        code = fault.findtext(f".//{{{NS_ZIMBRA}}}Code")
        raise ZimbraError(f"{op} : {reason}", code=code, http_status=r.status_code)
    return body[0]


class AdminSession:
    """Session admin : AuthRequest, renouvellement automatique du jeton à expiration."""

    RENEW_MARGIN_S = 120

    def __init__(self, settings: Settings, password: str):
        self.settings = settings
        self._password = password
        self._token: str | None = None
        self._expires_at = 0.0
        self.lifetime_ms = 0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<AdminSession {self.settings.admin_user}>"

    def authenticate(self) -> None:
        req = _el(NS_ADMIN, "AuthRequest")
        _el(NS_ADMIN, "account", req, self.settings.admin_user, by="name")
        _el(NS_ADMIN, "password", req, self._password)
        resp = soap_call(self.settings.admin_url, req, self.settings.ca_file)
        token = child_text(resp, "authToken")
        if not token:
            raise ZimbraError("AuthRequest : pas de authToken dans la réponse")
        self.lifetime_ms = int(child_text(resp, "lifetime") or 0)
        self._token = token
        self._expires_at = time.monotonic() + self.lifetime_ms / 1000
        log.info("AuthRequest admin OK pour %s (durée %d s)", self.settings.admin_user, self.lifetime_ms // 1000)

    def _get_token(self) -> str:
        with self._lock:
            if not self._token or time.monotonic() > self._expires_at - self.RENEW_MARGIN_S:
                self.authenticate()
            return self._token

    def call(self, body_el: ET.Element) -> ET.Element:
        try:
            return soap_call(self.settings.admin_url, body_el, self.settings.ca_file, self._get_token())
        except ZimbraError as e:
            if e.code not in AUTH_EXPIRED_CODES:
                raise
            log.info("Jeton admin expiré (%s), renouvellement", e.code)
            with self._lock:
                self._token = None
            return soap_call(self.settings.admin_url, body_el, self.settings.ca_file, self._get_token())

    def delegate_auth(self, email: str, duration_s: int = 3600) -> tuple[str, int]:
        """DelegateAuthRequest : jeton délégué neuf pour le compte (jeton, durée en ms)."""
        req = _el(NS_ADMIN, "DelegateAuthRequest", duration=str(duration_s))
        _el(NS_ADMIN, "account", req, email, by="name")
        resp = self.call(req)
        token = child_text(resp, "authToken")
        if not token:
            raise ZimbraError("DelegateAuthRequest : pas de authToken dans la réponse")
        return token, int(child_text(resp, "lifetime") or 0)

    def list_accounts(self, domain: str = "", page: int = 500) -> list[dict]:
        """SearchDirectoryRequest (lecture seule) : tous les comptes, hors comptes système."""
        out, offset = [], 0
        attrs = "displayName,uid,zimbraAccountStatus,zimbraIsSystemAccount,zimbraIsSystemResource"
        while True:
            kw = {"types": "accounts", "attrs": attrs, "limit": str(page), "offset": str(offset),
                  "sortBy": "name", "applyCos": "0", "applyConfig": "0"}
            if domain:
                kw["domain"] = domain
            resp = self.call(_el(NS_ADMIN, "SearchDirectoryRequest", **kw))
            batch = [c for c in resp if _local(c.tag) == "account"]
            for acc in batch:
                a = {x.get("n"): x.text for x in acc if _local(x.tag) == "a"}
                if (a.get("zimbraIsSystemAccount") or "").upper() == "TRUE" \
                        or (a.get("zimbraIsSystemResource") or "").upper() == "TRUE":
                    continue
                out.append({"email": (acc.get("name") or "").lower(), "id": acc.get("id"), "login": a.get("uid") or "",
                            "display_name": a.get("displayName") or "", "status": a.get("zimbraAccountStatus") or ""})
            offset += len(batch)
            if resp.get("more") not in ("1", "true") or not batch:
                return out

    def quota_usage(self, domain: str = "", page: int = 500) -> dict[str, dict]:
        """GetQuotaUsageRequest (lecture seule) : espace utilisé / quota de toutes les boîtes, en un appel paginé."""
        out, offset = {}, 0
        while True:
            kw = {"allServers": "1", "limit": str(page), "offset": str(offset), "sortBy": "account",
                  "sortAscending": "1", "refresh": "1"}
            if domain:
                kw["domain"] = domain
            resp = self.call(_el(NS_ADMIN, "GetQuotaUsageRequest", **kw))
            batch = [c for c in resp if _local(c.tag) == "account"]
            for a in batch:
                out[(a.get("name") or "").lower()] = {"used": int(a.get("used") or 0), "limit": int(a.get("limit") or 0)}
            offset += len(batch)
            if resp.get("more") not in ("1", "true") or not batch:
                return out

    def get_account(self, email: str) -> dict | None:
        """GetAccountRequest (lecture seule). None si le compte n'existe pas."""
        req = _el(NS_ADMIN, "GetAccountRequest", applyCos="0", attrs="zimbraAccountStatus")
        _el(NS_ADMIN, "account", req, email, by="name")
        try:
            resp = self.call(req)
        except ZimbraError as e:
            if e.code == "account.NO_SUCH_ACCOUNT":
                return None
            raise
        acc = next((c for c in resp if _local(c.tag) == "account"), None)
        if acc is None:
            raise ZimbraError("GetAccountRequest : pas d'élément account dans la réponse")
        attrs = {a.get("n"): a.text for a in acc if _local(a.tag) == "a"}
        return {"name": acc.get("name"), "id": acc.get("id"), "status": attrs.get("zimbraAccountStatus")}


class UserSession:
    """Accès à une boîte avec un jeton délégué neuf, renouvelé automatiquement s'il expire."""

    DURATION_S = 6 * 3600

    def __init__(self, admin: AdminSession, email: str, client: httpx.Client):
        self.admin = admin
        self.settings = admin.settings
        self.email = email
        self.client = client
        self._token: str | None = None
        self._lock = threading.Lock()  # la session est partagée par plusieurs envois parallèles

    def __repr__(self) -> str:
        return f"<UserSession {self.email}>"

    def renew(self, stale: str | None = None) -> None:
        """Nouveau jeton délégué (un seul renouvellement même si plusieurs envois expirent en même temps)."""
        with self._lock:
            if stale is not None and self._token != stale:
                return
            self._token, _ = self.admin.delegate_auth(self.email, self.DURATION_S)

    def _tok(self) -> str:
        if not self._token:
            self.renew()
        return self._token

    def soap(self, body_el: ET.Element) -> ET.Element:
        tok = self._tok()
        try:
            return soap_call(self.settings.user_soap_url, body_el, self.settings.ca_file, tok, self.client)
        except ZimbraError as e:
            if e.code not in AUTH_EXPIRED_CODES:
                raise
            log.info("Jeton délégué expiré pour %s, renouvellement", self.email)
            self.renew(stale=tok)
            return soap_call(self.settings.user_soap_url, body_el, self.settings.ca_file, self._tok(), self.client)

    def _http(self, url: str, data: bytes | None = None, headers: dict | None = None,
              files: list | None = None) -> httpx.Response:
        """POST authentifié par cookie ; renouvelle le jeton une fois sur 401."""
        for attempt in (1, 2):
            tok = self._tok()
            h = {**(headers or {}), "Cookie": f"ZM_AUTH_TOKEN={tok}"}
            try:
                r = self.client.post(url, content=data, headers=h, files=files)
            except httpx.HTTPError as e:
                raise ZimbraError(f"POST {url.split('?')[0]} : {type(e).__name__} : {e}") from None
            if r.status_code == 401 and attempt == 1:
                self.renew(stale=tok)
                continue
            return r
        return r

    def get_info(self) -> dict:
        return _info_from(self.soap(_el(NS_ACCOUNT, "GetInfoRequest", sections="mbox")))

    def create_folder(self, name: str, parent_id: str, view: str) -> tuple[str, str]:
        """CreateFolderRequest avec fie=1 (retourne le dossier existant s'il existe déjà)."""
        req = _el(NS_MAIL, "CreateFolderRequest")
        _el(NS_MAIL, "folder", req, name=name, l=parent_id, view=view, fie="1")
        resp = self.soap(req)
        f = next((c for c in resp if _local(c.tag) == "folder"), None)
        if f is None or not f.get("id"):
            raise ZimbraError(f"CreateFolderRequest {name} : réponse sans dossier")
        return f.get("id"), f.get("absFolderPath") or ""

    def folder_tree(self) -> list[dict]:
        """GetFolderRequest (lecture seule) : tous les dossiers de la boîte avec nombre d'éléments et taille."""
        resp = self.soap(_el(NS_MAIL, "GetFolderRequest"))
        out = []

        def walk(el: ET.Element) -> None:
            for c in el:
                if _local(c.tag) != "folder":
                    continue  # recherches enregistrées, partages : ignorés
                if c.get("id") != "1":
                    out.append({"id": c.get("id"), "path": c.get("absFolderPath") or "/" + (c.get("name") or ""),
                                "view": c.get("view") or "message", "n": int(c.get("n") or 0),
                                "size": int(c.get("s") or 0), "unread": int(c.get("u") or 0)})
                walk(c)

        walk(resp)
        return out

    def search_ids(self, types: str, header: str | None = None, page: int = 1000) -> tuple[set[str], int, int]:
        """SearchRequest (lecture seule) sur toute la boîte : identifiants présents.
        Mails : en-tête demandé (Message-ID) ; rendez-vous / tâches : attribut uid.
        Retourne (identifiants, nombre de résultats, nombre de résultats porteurs de l'identifiant)."""
        ids, hits, with_id, offset = set(), 0, 0, 0
        while True:
            req = _el(NS_MAIL, "SearchRequest", types=types, limit=str(page), offset=str(offset), sortBy="dateAsc")
            _el(NS_MAIL, "query", req, "is:anywhere")
            if header:
                _el(NS_MAIL, "header", req, n=header)
            resp = self.soap(req)
            batch = [c for c in resp if _local(c.tag) in ("m", "appt", "task", "c")]
            for c in batch:
                hits += 1
                if header:
                    v = next((h.text for h in c if _local(h.tag) == "header" and (h.get("n") or "").lower() == header.lower()), None)
                else:
                    v = c.get("uid")
                if v and v.strip():
                    ids.add(v.strip())
                    with_id += 1
            offset += len(batch)
            if resp.get("more") not in ("1", "true") or not batch:
                return ids, hits, with_id

    def folder_count(self, folder_id: str) -> int:
        req = _el(NS_MAIL, "GetFolderRequest", depth="0")
        _el(NS_MAIL, "folder", req, l=folder_id)
        f = next((c for c in self.soap(req) if _local(c.tag) == "folder"), None)
        return int(f.get("n") or 0) if f is not None else 0

    def upload(self, data: bytes) -> str:
        """/service/upload (brut) -> identifiant de pièce (aid)."""
        r = self._http(self.settings.user_url.rstrip("/") + "/service/upload?fmt=extended,raw", data, {
            "Content-Type": "message/rfc822",
            "Content-Disposition": 'attachment; filename="message.eml"',
        })
        m = re.match(r"\s*(\d+)\s*,", r.text)
        aid = re.search(r'"aid"\s*:\s*"([^"]+)"', r.text)
        if r.status_code != 200 or not m or m.group(1) != "200" or not aid:
            raise ZimbraError(f"upload : {r.text[:200].strip()}", http_status=r.status_code)
        return aid.group(1)

    def upload_many(self, messages: list[bytes]) -> list[str]:
        """Un seul /service/upload (multipart) pour plusieurs messages -> aids dans le même ordre."""
        files = [("file", (f"m{i}.eml", data, "message/rfc822")) for i, data in enumerate(messages)]
        r = self._http(self.settings.user_url.rstrip("/") + "/service/upload?fmt=extended", files=files)
        m = re.match(r"\s*(\d+)\s*,", r.text)
        aids = re.findall(r'"aid"\s*:\s*"([^"]+)"', r.text)
        if r.status_code != 200 or not m or m.group(1) != "200" or len(aids) != len(messages):
            raise ZimbraError(f"upload groupé ({len(messages)} messages) : {r.text[:200].strip()}",
                              http_status=r.status_code)
        return aids

    @staticmethod
    def _add_msg_el(folder_id: str, aid: str, flags: str, date_ms: int | None, **extra) -> ET.Element:
        req = _el(NS_MAIL, "AddMsgRequest", **extra)
        attrs = {"l": folder_id, "aid": aid, "noICal": "1"}
        if flags:
            attrs["f"] = flags
        if date_ms:
            attrs["d"] = str(date_ms)
        _el(NS_MAIL, "m", req, **attrs)
        return req

    def add_msg(self, folder_id: str, aid: str, flags: str, date_ms: int | None) -> str:
        m = next((c for c in self.soap(self._add_msg_el(folder_id, aid, flags, date_ms)) if _local(c.tag) == "m"), None)
        return m.get("id") if m is not None else ""

    def add_msgs(self, msgs: list[tuple[str, str, str, int | None]]) -> list[tuple[str | None, str | None]]:
        """Plusieurs AddMsgRequest dans une seule BatchRequest (onerror=continue).
        Retourne, dans l'ordre, (id Zimbra, None) ou (None, erreur) pour chaque message."""
        batch = _el(NS_ZIMBRA, "BatchRequest", onerror="continue")
        for i, (folder_id, aid, flags, date_ms) in enumerate(msgs):
            batch.append(self._add_msg_el(folder_id, aid, flags, date_ms, requestId=str(i)))
        resp = self.soap(batch)
        results: list[tuple[str | None, str | None]] = [(None, "pas de réponse")] * len(msgs)
        for child in resp:
            rid = child.get("requestId")
            if rid is None or not rid.isdigit() or int(rid) >= len(msgs):
                continue
            if _local(child.tag) == "Fault":
                reason = child.findtext(f".//{{{SOAP_NS}}}Text") or "Fault SOAP"
                code = child.findtext(f".//{{{NS_ZIMBRA}}}Code")
                results[int(rid)] = (None, f"{code} : {reason}" if code else reason)
            else:
                m = next((c for c in child if _local(c.tag) == "m"), None)
                results[int(rid)] = (m.get("id") if m is not None else "", None)
        return results

    def rest_import(self, folder_path: str, fmt: str, data: bytes, content_type: str) -> None:
        """POST REST (ics / vcf) dans un dossier : /home/<email>/<chemin>?fmt=..."""
        path = "/".join(quote(p, safe="") for p in folder_path.strip("/").split("/"))
        url = f"{self.settings.user_url.rstrip('/')}/home/{quote(self.email, safe='@')}/{path}?fmt={fmt}"
        r = self._http(url, data, {"Content-Type": content_type})
        if r.status_code >= 300:
            raise ZimbraError(f"import {fmt} vers {folder_path} : {r.text[:300].strip()}", http_status=r.status_code)


def _info_from(resp: ET.Element) -> dict:
    return {
        "name": child_text(resp, "name"),
        "id": child_text(resp, "id"),
        "used": child_text(resp, "used"),
        "version": child_text(resp, "version"),
        "rest": child_text(resp, "rest"),
    }


def get_info(settings: Settings, delegated_token: str) -> dict:
    """GetInfoRequest (lecture seule) sur l'URL utilisateur avec le jeton délégué."""
    req = _el(NS_ACCOUNT, "GetInfoRequest", sections="mbox")
    return _info_from(soap_call(settings.user_soap_url, req, settings.ca_file, delegated_token))
