"""SQLite CONTROL routing smoke test with an empty temporary database."""
from http.client import HTTPResponse
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from control import app


class Socket:
    def __init__(self, payload):
        self.payload = payload
        self.output = BytesIO()

    def makefile(self, mode, *args):
        return BytesIO(self.payload)

    def sendall(self, data):
        self.output.write(data)


def request(server, route):
    socket = Socket(f"GET {route} HTTP/1.0\r\nHost: ernasa.com\r\n\r\n".encode())
    app.Handler(socket, ("127.0.0.1", 40000), server)
    response = HTTPResponse(Socket(socket.output.getvalue()))
    response.begin()
    return response.status, dict(response.getheaders()), response.read()


with TemporaryDirectory() as directory:
    database = Path(directory) / "control.sqlite3"
    app.init(database)
    server = SimpleNamespace(
        db_path=database, origin="https://ernasa.com", public_prefix="/control"
    )
    status, headers, _ = request(server, "/")
    assert (status, headers.get("Location")) == (303, "/control/giris")
    status, headers, page = request(server, "/giris")
    assert status == 200
    assert b'href="/control/control.css"' in page
    assert b'action="/control/giris"' in page
    assert "Path=/control/giris" in headers["Set-Cookie"]
    assert "; Secure" in headers["Set-Cookie"]
    assert request(server, "/control.css")[0] == 200
print("CONTROL SQLite smoke test passed")
