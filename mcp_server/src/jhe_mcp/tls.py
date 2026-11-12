"""TLS for the HTTP server, from the environment.

A load balancer that keeps traffic encrypted to the container needs the container to speak
HTTPS. The hop is encrypted, not mutually authenticated: an ALB does not validate the
certificate it is shown, and the VPC is what vouches for the container, so a self-signed
certificate issued at start, for this container only, does the job; a deployment with a CA
mounts its own files instead.

    MCP_TLS unset         plain HTTP (local runs, compose).
    MCP_TLS=self-signed   a key pair and certificate generated here into MCP_TLS_DIR (default: a
                          fresh temporary directory). MCP_TLS_CN sets the name, MCP_TLS_DAYS the
                          validity: ten years by default, since the key lives and dies with the
                          container and a long-running task must never present an expired
                          certificate to anything that checks.
    MCP_TLS=provided      MCP_TLS_CERTFILE and MCP_TLS_KEYFILE name mounted files.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TlsMode(StrEnum):
    SELF_SIGNED = "self-signed"
    PROVIDED = "provided"


@dataclass(frozen=True)
class TlsFiles:
    certfile: Path
    keyfile: Path


def resolve_tls(environ: dict[str, str] | None = None) -> TlsFiles | None:
    """The certificate and key the server should serve with, or None for plain HTTP.
    Raises RuntimeError for a mode it does not know or a provided mode missing its files."""
    env = os.environ if environ is None else environ
    raw = env.get("MCP_TLS", "").strip()
    if not raw:
        return None
    try:
        mode = TlsMode(raw)
    except ValueError:
        raise RuntimeError(f"MCP_TLS must be unset, 'self-signed' or 'provided' (got {raw!r})") from None
    if mode is TlsMode.PROVIDED:
        certfile, keyfile = env.get("MCP_TLS_CERTFILE"), env.get("MCP_TLS_KEYFILE")
        if not certfile or not keyfile:
            raise RuntimeError("MCP_TLS=provided needs MCP_TLS_CERTFILE and MCP_TLS_KEYFILE")
        return TlsFiles(Path(certfile), Path(keyfile))
    directory = Path(env.get("MCP_TLS_DIR") or tempfile.mkdtemp(prefix="mcp-tls-"))
    return self_signed(
        directory,
        common_name=env.get("MCP_TLS_CN", "jhe-mcp"),
        days=int(env.get("MCP_TLS_DAYS", "3650")),
    )


def self_signed(directory: Path, common_name: str = "jhe-mcp", days: int = 3650) -> TlsFiles:
    """Write a fresh P-256 key and a self-signed certificate for `common_name` into `directory`."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    keyfile, certfile = directory / "key.pem", directory / "cert.pem"
    keyfile.touch(mode=0o600)
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return TlsFiles(certfile, keyfile)
