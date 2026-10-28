"""Tests de connectivité : DNS, TCP, certificat TLS, AuthRequest admin."""
import socket
import ssl
from datetime import datetime, timezone

from cryptography import x509

from .config import Settings
from .zimbra import AdminSession, ZimbraError, ssl_context

TIMEOUT_S = 8
CERT_WARN_DAYS = 30


def _result(label: str, status: str, detail: str, lines: list[str] | None = None) -> dict:
    return {"label": label, "status": status, "detail": detail, "lines": lines or []}


def check_dns(host: str) -> dict:
    label = f"DNS {host}"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return _result(label, "KO", f"gaierror {e.errno} : {e.strerror}")
    ips = sorted({i[4][0] for i in infos})
    return _result(label, "OK", ", ".join(ips))


def check_tcp(host: str, port: int) -> dict:
    label = f"TCP {host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S) as s:
            peer = s.getpeername()[0]
    except OSError as e:
        return _result(label, "KO", f"{type(e).__name__} : {e}")
    return _result(label, "OK", f"connexion établie ({peer})")


def _cert_lines(der: bytes) -> tuple[list[str], int]:
    cert = x509.load_der_x509_certificate(der)
    not_after = cert.not_valid_after_utc
    days = (not_after - datetime.now(timezone.utc)).days
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = ", ".join(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        names = "(aucun)"
    lines = [
        f"Sujet : {cert.subject.rfc4514_string()}",
        f"Émetteur : {cert.issuer.rfc4514_string()}",
        f"SAN : {names}",
        f"Valide du {cert.not_valid_before_utc:%Y-%m-%d} au {not_after:%Y-%m-%d %H:%M} UTC ({days} jours restants)",
    ]
    return lines, days


def _fetch_der(host: str, port: int, ctx: ssl.SSLContext) -> tuple[bytes, str]:
    with socket.create_connection((host, port), timeout=TIMEOUT_S) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            return s.getpeercert(binary_form=True), s.version() or ""


def check_tls(host: str, port: int, ca_file: str) -> dict:
    label = f"Certificat TLS {host}:{port}"
    try:
        der, version = _fetch_der(host, port, ssl_context(ca_file))
    except ssl.SSLCertVerificationError as e:
        # Vérification échouée : on le signale KO. Le certificat est relu sans vérification
        # uniquement pour afficher émetteur/expiration ; aucune requête Zimbra n'utilise ce mode.
        detail = f"Vérification échouée : {e.verify_message} (code {e.verify_code})"
        lines = []
        try:
            diag = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            diag.check_hostname = False
            diag.verify_mode = ssl.CERT_NONE
            der, _ = _fetch_der(host, port, diag)
            lines = ["(certificat lu sans vérification, pour diagnostic uniquement)"] + _cert_lines(der)[0]
        except Exception as e2:  # noqa: BLE001
            lines = [f"Lecture du certificat impossible : {type(e2).__name__} : {e2}"]
        return _result(label, "KO", detail, lines)
    except (OSError, ssl.SSLError) as e:
        return _result(label, "KO", f"{type(e).__name__} : {e}")

    lines, days = _cert_lines(der)
    if days < 0:
        return _result(label, "KO", "Certificat expiré", lines)
    status = "AVERT" if days < CERT_WARN_DAYS else "OK"
    return _result(label, status, f"Chaîne et nom vérifiés ({version})", lines)


def check_admin_auth(settings: Settings, password: str | None) -> tuple[dict, AdminSession | None]:
    label = f"AuthRequest admin ({settings.admin_user or 'compte non défini'})"
    if not settings.admin_user or not password:
        return _result(label, "—", "Compte admin ou mot de passe non saisi (écran Configuration)"), None
    session = AdminSession(settings, password)
    try:
        session.authenticate()
    except ZimbraError as e:
        return _result(label, "KO", str(e)), None
    hours = session.lifetime_ms / 3_600_000
    return _result(label, "OK", f"Jeton admin obtenu (durée {hours:.1f} h, non affiché)"), session


def run_all(settings: Settings, password: str | None) -> tuple[list[dict], AdminSession | None]:
    results = [
        check_dns(settings.admin_host),
        check_dns(settings.user_host),
        check_tcp(settings.admin_host, settings.admin_port),
        check_tcp(settings.user_host, settings.user_port),
        check_tls(settings.admin_host, settings.admin_port, settings.ca_file),
        check_tls(settings.user_host, settings.user_port, settings.ca_file),
    ]
    auth, session = check_admin_auth(settings, password)
    results.append(auth)
    return results, session
