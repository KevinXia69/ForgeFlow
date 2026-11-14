"""Exercise an actual TLS connection; a local install never disables verification."""
from __future__ import annotations

import ipaddress
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from forgeflow.semantic.config import SemanticConfig, SemanticConfigError
from forgeflow.semantic.http import semantic_client


def config(**overrides):
    return SemanticConfig(**{
        "qdrant_url": "https://qdrant:6333", "qdrant_api_key": "test-local-qdrant",
        "embedding_url": "https://embedding.example/v1/embeddings", "embedding_model": "test",
        "embedding_api_key": "test-only", "reranker_url": "https://rerank.example/v1/rerank",
        "reranker_model": "test", "reranker_api_key": "test-only", **overrides,
    })


@pytest.fixture
def tls_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local test")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified-local-service")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cert_path, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_explicit_ca_trusts_own_service_but_still_checks_hostname(tls_server, monkeypatch):
    ca, port = tls_server
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with semantic_client(config(ca_file=str(ca)), timeout=3) as client:
        assert client.get(f"https://127.0.0.1:{port}").text == "verified-local-service"
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            client.get(f"https://localhost:{port}")
    with semantic_client(config(), timeout=3) as client:
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            client.get(f"https://127.0.0.1:{port}")


def test_invalid_explicit_ca_fails_closed(tmp_path):
    for value in ("relative.pem", str(tmp_path / "missing.pem")):
        with pytest.raises(SemanticConfigError, match="semantic_ca_file_invalid"):
            semantic_client(config(ca_file=value), timeout=1)
    invalid = tmp_path / "invalid.pem"
    invalid.write_text("not a certificate")
    with pytest.raises(SemanticConfigError, match="semantic_ca_file_invalid"):
        semantic_client(config(ca_file=str(invalid)), timeout=1)


def test_plain_http_remains_rejected():
    with pytest.raises(SemanticConfigError, match="absolute HTTPS"):
        config(qdrant_url="http://qdrant:6333")
