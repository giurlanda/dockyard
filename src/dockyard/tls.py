"""The local listener's certificate: the Administrator's own, else one Dockyard makes itself.

Both live in the data volume's TLS folder. The self-signed one is made once and kept, so browsers
do not warn again after every restart; only when it expires is a new one made.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .settings import LocalSignIn

TLS_FOLDER = "tls"
PROVIDED = ("cert.pem", "key.pem")
SELF_SIGNED = ("self-signed-cert.pem", "self-signed-key.pem")
# The longest validity browsers accept, on macOS and iOS, for a certificate a person trusts.
SELF_SIGNED_VALIDITY = timedelta(days=825)


class CertificateError(Exception):
    """The certificate in the TLS folder cannot be used."""


@dataclass(frozen=True)
class Certificate:
    certfile: Path
    keyfile: Path
    fingerprint: str  # SHA-256, as browsers show it: AB:CD:...
    provided: bool  # by the Administrator; else self-signed by Dockyard


def local_certificate(data_dir: Path, local: LocalSignIn) -> Certificate:
    """The provided certificate if there is one, else the self-signed one, made if missing or
    expired. Raises CertificateError."""
    folder = data_dir / TLS_FOLDER
    certfile, keyfile = (folder / name for name in PROVIDED)
    if certfile.exists() or keyfile.exists():
        if not (certfile.exists() and keyfile.exists()):
            raise CertificateError(
                f"Local sign-in needs both {certfile} and {keyfile} (PEM), or neither to have "
                "Dockyard make a self-signed certificate."
            )
        return Certificate(certfile, keyfile, _checked(certfile, keyfile), provided=True)

    certfile, keyfile = (folder / name for name in SELF_SIGNED)
    if not (certfile.exists() and keyfile.exists()) or _expired(certfile):
        _make_self_signed(certfile, keyfile, local)
    return Certificate(certfile, keyfile, _checked(certfile, keyfile), provided=False)


def _checked(certfile: Path, keyfile: Path) -> str:
    """The certificate's fingerprint, once it is known to go with the key."""
    try:
        certificate = x509.load_pem_x509_certificate(certfile.read_bytes())
        key = serialization.load_pem_private_key(keyfile.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as error:
        raise CertificateError(
            f"Cannot read the Local sign-in certificate {certfile} and its key {keyfile} "
            f"(PEM, key without a passphrase): {error}"
        ) from error
    if key.public_key() != certificate.public_key():
        raise CertificateError(f"The key {keyfile} does not belong to the certificate {certfile}.")
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def _expired(certfile: Path) -> bool:
    try:
        certificate = x509.load_pem_x509_certificate(certfile.read_bytes())
    except ValueError:
        return True
    return certificate.not_valid_after_utc <= datetime.now(UTC)


def _make_self_signed(certfile: Path, keyfile: Path, local: LocalSignIn) -> None:
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    if not local.on_every_interface:
        names.append(x509.IPAddress(local.address))
    if local.url and (host := urlsplit(local.url).hostname):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Dockyard Local sign-in")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + SELF_SIGNED_VALIDITY)
        .add_extension(x509.SubjectAlternativeName(list(dict.fromkeys(names))), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    try:
        certfile.parent.mkdir(parents=True, exist_ok=True)
        _write(
            keyfile,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        _write(certfile, certificate.public_bytes(serialization.Encoding.PEM))
    except OSError as error:
        raise CertificateError(
            f"Cannot store the self-signed Local sign-in certificate in {certfile.parent}: {error}"
        ) from error


def _write(path: Path, content: bytes) -> None:
    """Readable by Dockyard only, and never half-written."""
    partial = path.with_name(f".{path.name}.partial")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(content)
    partial.replace(path)
