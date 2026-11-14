"""CI-only deterministic embedding transport; never a production model service."""
from __future__ import annotations

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ci-fixture-not-a-model")

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 4 * 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        if self.path == "/v1/embeddings":
            rows = body["input"]
            if isinstance(rows, str):
                rows = [rows]
            result = {"data": [{"index": i, "embedding": [1.0] + [0.0] * 4095}
                               for i, _ in enumerate(rows)]}
        elif self.path == "/v1/rerank":
            result = {"results": [{"index": i, "relevance_score": 1 / (i + 1)}
                                  for i, _ in enumerate(body["documents"])]}
        else:
            self.send_error(404)
            return
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain("/etc/forgeflow/cert.pem", "/etc/forgeflow/key.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
