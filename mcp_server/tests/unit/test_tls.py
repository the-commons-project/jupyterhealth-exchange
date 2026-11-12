import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from jhe_mcp.tls import TlsFiles, resolve_tls, self_signed


def test_unset_means_plain_http():
    assert resolve_tls({}) is None
    assert resolve_tls({"MCP_TLS": "  "}) is None


def test_unknown_mode_is_refused():
    with pytest.raises(RuntimeError, match="MCP_TLS must be"):
        resolve_tls({"MCP_TLS": "yes"})


def test_provided_needs_both_files(tmp_path: Path):
    with pytest.raises(RuntimeError, match="MCP_TLS_CERTFILE and MCP_TLS_KEYFILE"):
        resolve_tls({"MCP_TLS": "provided", "MCP_TLS_CERTFILE": str(tmp_path / "c.pem")})
    files = resolve_tls(
        {"MCP_TLS": "provided", "MCP_TLS_CERTFILE": "/etc/tls/c.pem", "MCP_TLS_KEYFILE": "/etc/tls/k.pem"}
    )
    assert files == TlsFiles(Path("/etc/tls/c.pem"), Path("/etc/tls/k.pem"))


def test_self_signed_writes_a_usable_pair(tmp_path: Path):
    files = resolve_tls({"MCP_TLS": "self-signed", "MCP_TLS_DIR": str(tmp_path), "MCP_TLS_CN": "mcp-task"})
    assert files is not None and files.certfile.parent == tmp_path
    assert files.keyfile.stat().st_mode & 0o077 == 0, "the key is readable by its owner only"
    cert = x509.load_pem_x509_certificate(files.certfile.read_bytes())
    assert cert.subject == cert.issuer, "self-signed"
    assert cert.subject.rfc4514_string() == "CN=mcp-task"
    assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= 3650, "outlives any task"
    # The pair loads into an SSL context, which is what uvicorn does with it.
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(files.certfile, files.keyfile)


def test_self_signed_defaults_to_a_fresh_temporary_directory():
    a, b = resolve_tls({"MCP_TLS": "self-signed"}), resolve_tls({"MCP_TLS": "self-signed"})
    assert a is not None and b is not None and a.certfile.parent != b.certfile.parent
    for files in (a, b):
        files.certfile.unlink()
        files.keyfile.unlink()
        files.certfile.parent.rmdir()


def test_self_signed_directly(tmp_path: Path):
    files = self_signed(tmp_path / "nested", common_name="x", days=1)
    assert files.certfile.exists() and files.keyfile.exists()
