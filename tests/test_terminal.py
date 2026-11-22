"""Tests for the "Terminal" menu item: the LXD exec session behind it.

TerminalSession is driven with fake websockets and a mocked pylxd client.
_ExecWebsocket, the real websocket client, is run against a small websocket
server on a local socket, with and without TLS, so the handshake, the frames
and the certificate pinning are exercised for real.
"""

import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from ws4py.websocket import WebSocket

from test_indicator import FakeInstance, FakeServers, IndicatorTestCase, LOCAL, ind

OPERATION = "/1.0/operations/op-1234"
FDS = {"0": "data-secret", "control": "control-secret"}


def make_client(websocket_url="ws+unix:///var/snap/lxd/common/lxd/unix.socket"):
    """A mocked pylxd Client that accepts one exec request."""
    client = mock.MagicMock(name="Client")
    client.websocket_url = websocket_url
    client.cert = None
    client.api.session.verify = True
    exec_node = client.api.instances.__getitem__.return_value.__getitem__
    exec_node.return_value.post.return_value.json.return_value = {
        "operation": OPERATION,
        "metadata": {"metadata": {"fds": FDS}},
    }
    operation = client.api.operations.__getitem__.return_value
    operation.websocket._api_endpoint = (
        f"http+unix://%2Fsocket{OPERATION}/websocket?project=default"
    )
    operation.wait.get.return_value.json.return_value = {
        "metadata": {"metadata": {"return": 0}}
    }
    return client


def exec_post(client):
    return client.api.instances.__getitem__.return_value.__getitem__.return_value.post


class FakeWebsocket:
    def __init__(self, url, resource, on_message, on_closed, ssl_options, pinned):
        self.url, self.resource = url, resource
        self.on_message, self.on_closed = on_message, on_closed
        self.ssl_options, self.pinned = ssl_options, pinned
        self.sent = []
        self.connected = self.closed = False
        self.fail_connect = None

    def connect(self):
        if self.fail_connect:
            raise self.fail_connect
        self.connected = True

    def send(self, payload, binary=False):
        self.sent.append((payload, binary))

    def close(self):
        self.closed = True


class FakeWebsocketFactory:
    def __init__(self):
        self.created = []
        self.fail = {}  # secret -> exception raised by connect()
        self.on_create = None

    def __call__(self, *args):
        ws = FakeWebsocket(*args)
        ws.fail_connect = self.fail.get(ws.resource.split("secret=")[1])
        self.created.append(ws)
        if self.on_create:
            self.on_create(ws)
        return ws

    def by_secret(self, secret):
        (ws,) = [w for w in self.created if w.resource.endswith(f"secret={secret}")]
        return ws

    @property
    def data(self):
        return self.by_secret("data-secret")

    @property
    def control(self):
        return self.by_secret("control-secret")


def message(data):
    return types.SimpleNamespace(data=data)


def control_messages(ws):
    return [json.loads(payload) for payload, _binary in ws.sent]


class TerminalSessionTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch("builtins.print")
        patch.start()
        self.addCleanup(patch.stop)
        self.client = make_client()
        self.factory = FakeWebsocketFactory()
        self.output = []
        self.exits = []

    def make_session(self, **kwargs):
        return ind.TerminalSession(
            self.client,
            "web",
            on_output=self.output.append,
            on_exit=self.exits.append,
            websocket_factory=self.factory,
            **kwargs,
        )

    def test_start_requests_an_interactive_shell_sized_to_the_terminal(self):
        self.make_session(columns=132, rows=43).start()
        self.client.api.instances.__getitem__.assert_called_with("web")
        exec_node = self.client.api.instances.__getitem__.return_value
        exec_node.__getitem__.assert_called_with("exec")
        request = exec_post(self.client).call_args.kwargs["json"]
        self.assertEqual(
            request,
            {
                "command": ind.TERMINAL_COMMAND,
                "environment": {"TERM": "xterm-256color"},
                "interactive": True,
                "wait-for-websocket": True,
                "width": 132,
                "height": 43,
            },
        )

    def test_shell_command_is_a_login_shell_like_lxc_shell(self):
        self.assertEqual(ind.TERMINAL_COMMAND[:2], ["sh", "-c"])
        self.assertIn("exec su -l", ind.TERMINAL_COMMAND[2])

    def test_shell_command_falls_back_to_sh_without_su(self):
        script = ind.TERMINAL_COMMAND[2]
        with tempfile.TemporaryDirectory() as bin_dir:
            # A PATH with sh but no su, and an sh that reports how it was run.
            sh = os.path.join(bin_dir, "sh")
            with open(sh, "w") as f:
                f.write('#!/bin/sh\necho "sh $*"\n')
            os.chmod(sh, 0o755)
            result = subprocess.run(
                ["/bin/sh", "-c", script],
                env={"PATH": bin_dir},
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.stdout, "sh -l\n")

    def test_shell_command_uses_su_when_present(self):
        script = ind.TERMINAL_COMMAND[2]
        with tempfile.TemporaryDirectory() as bin_dir:
            for name in ("su", "sh"):
                path = os.path.join(bin_dir, name)
                with open(path, "w") as f:
                    f.write(f'#!/bin/sh\necho "{name} $*"\n')
                os.chmod(path, 0o755)
            result = subprocess.run(
                ["/bin/sh", "-c", script],
                env={"PATH": bin_dir},
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.stdout, "su -l\n")

    def test_start_connects_control_then_data_websockets(self):
        session = self.make_session()
        session.start()
        self.assertEqual(session.operation_id, "op-1234")
        self.client.api.operations.__getitem__.assert_called_with("op-1234")
        self.assertEqual(
            [ws.resource for ws in self.factory.created],
            [
                f"{OPERATION}/websocket?secret=control-secret",
                f"{OPERATION}/websocket?secret=data-secret",
            ],
        )
        for ws in self.factory.created:
            self.assertTrue(ws.connected)
            self.assertEqual(ws.url, self.client.websocket_url)
            self.assertEqual((ws.ssl_options, ws.pinned), ({}, None))
        # Nothing to say yet: the size went with the exec request.
        self.assertEqual(self.factory.control.sent, [])

    def test_input_is_sent_as_binary_on_the_data_websocket(self):
        session = self.make_session()
        session.start()
        session.send_input(b"ls\r")
        self.assertEqual(self.factory.data.sent, [(b"ls\r", True)])

    def test_input_typed_while_connecting_is_sent_once_connected(self):
        session = self.make_session()
        session.send_input(b"early ")

        def type_more(ws):
            if ws.resource.endswith("data-secret"):
                session.send_input(b"bird")

        self.factory.on_create = type_more
        session.start()
        self.assertEqual(self.factory.data.sent, [(b"early ", True), (b"bird", True)])

    def test_resize_sends_window_resize_on_the_control_websocket(self):
        session = self.make_session(columns=80, rows=24)
        session.start()
        session.resize(100, 30)
        session.resize(100, 30)  # unchanged: not sent again
        self.assertEqual(
            control_messages(self.factory.control),
            [
                {
                    "command": "window-resize",
                    "args": {"width": "100", "height": "30"},
                }
            ],
        )
        self.assertEqual(self.factory.control.sent[0][1], False)  # a text frame

    def test_resize_while_connecting_is_sent_once_connected(self):
        session = self.make_session(columns=80, rows=24)
        session.resize(120, 40)
        session.start()
        self.assertEqual(exec_post(self.client).call_args.kwargs["json"]["width"], 120)
        self.assertEqual(self.factory.control.sent, [])

        session = self.make_session(columns=80, rows=24)
        self.factory.created.clear()
        self.factory.on_create = lambda ws: session.resize(90, 20)
        session.start()
        self.assertEqual(
            control_messages(self.factory.control)[-1]["args"],
            {"width": "90", "height": "20"},
        )

    def test_output_is_passed_on_as_bytes(self):
        session = self.make_session()
        session.start()
        self.factory.data.on_message(message(bytearray(b"root@web:~# ")))
        self.factory.data.on_message(message(b""))
        self.factory.data.on_message(message(None))
        self.assertEqual(self.output, [b"root@web:~# "])
        self.assertIsInstance(self.output[0], bytes)

    def test_shell_exit_reports_the_exit_status_once(self):
        session = self.make_session()
        session.start()
        operation = self.client.api.operations.__getitem__.return_value
        operation.wait.get.return_value.json.return_value = {
            "metadata": {"metadata": {"return": 130}}
        }
        self.factory.data.on_closed()
        self.factory.data.on_closed()
        self.assertEqual(self.exits, [130])
        self.assertTrue(self.factory.control.closed)
        operation.wait.get.assert_called_once_with(params={"timeout": 3})

    def test_lost_connection_reports_unknown_exit_status(self):
        session = self.make_session()
        session.start()
        operation = self.client.api.operations.__getitem__.return_value
        operation.wait.get.side_effect = ConnectionError("gone")
        self.factory.data.on_closed()
        self.assertEqual(self.exits, [None])

    def test_close_hangs_up_the_shell_and_disconnects(self):
        session = self.make_session()
        session.start()
        session.close()
        self.assertEqual(
            control_messages(self.factory.control), [{"command": "signal", "signal": 1}]
        )
        self.assertTrue(self.factory.data.closed)
        self.assertTrue(self.factory.control.closed)
        session.send_input(b"too late")  # goes to the closed socket, harmlessly

    def test_close_after_the_shell_exited_does_not_signal(self):
        session = self.make_session()
        session.start()
        self.factory.data.on_closed()
        session.close()
        self.assertEqual(self.factory.control.sent, [])

    def test_close_while_connecting_disconnects_once_connected(self):
        session = self.make_session()
        self.factory.on_create = lambda ws: session.close()
        session.start()
        self.assertTrue(self.factory.data.closed)
        self.assertTrue(self.factory.control.closed)
        self.assertEqual(self.factory.data.sent, [])

    def test_input_after_close_before_connecting_is_dropped(self):
        session = self.make_session()
        session.close()
        session.send_input(b"x")
        self.assertEqual(session._pending, [])

    def test_failed_connect_disconnects_and_raises(self):
        self.factory.fail["data-secret"] = ConnectionRefusedError("refused")
        session = self.make_session()
        with self.assertRaises(ConnectionRefusedError):
            session.start()
        self.assertTrue(self.factory.control.closed)

    def test_failed_exec_request_raises(self):
        exec_post(self.client).side_effect = ind.LXDAPIException(
            mock.Mock(
                status_code=400, json=mock.Mock(return_value={"error": "not running"})
            )
        )
        with self.assertRaisesRegex(ind.LXDAPIException, "not running"):
            self.make_session().start()
        self.assertEqual(self.factory.created, [])

    def test_send_failures_are_swallowed(self):
        session = self.make_session()
        session.start()
        self.factory.data.send = mock.Mock(side_effect=BrokenPipeError())
        session.send_input(b"x")  # no exception


def make_certificate(directory, name):
    """Writes a self-signed certificate and key; returns their paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, f"{name}.crt")
    key_path = os.path.join(directory, f"{name}.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


class WebsocketSecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unix_socket_needs_no_tls(self):
        self.assertEqual(ind._websocket_security(make_client()), ({}, None))

    def test_pinned_server_certificate_is_checked_by_fingerprint(self):
        server_cert, _key = make_certificate(self.tmp.name, "zeta")
        client = make_client("wss://zeta:8443")
        client.cert = ("/conf/client.crt", "/conf/client.key")
        client.api.session.verify = server_cert
        options, pinned = ind._websocket_security(client)
        self.assertEqual(
            options, {"certfile": "/conf/client.crt", "keyfile": "/conf/client.key"}
        )
        with open(server_cert) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
        self.assertEqual(pinned, hashlib.sha256(der).hexdigest())

    def test_unpinned_server_is_checked_against_the_cas(self):
        client = make_client("wss://zeta:8443")
        options, pinned = ind._websocket_security(client)
        self.assertIsNone(pinned)
        self.assertEqual(options["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertTrue(os.path.exists(options["ca_certs"]))
        self.assertNotIn("certfile", options)

    def test_disabled_verification_stays_disabled(self):
        client = make_client("wss://zeta:8443")
        client.api.session.verify = False
        self.assertEqual(ind._websocket_security(client), ({}, None))


def shut_down(ws, thread):
    """Closes a websocket cleanly and waits for its reader thread to end."""
    if not ws.terminated:
        try:
            ws.close()
        except Exception:
            pass
    if thread.ident is not None:
        thread.join(5)
    if ws.sock is not None:
        ws.close_connection()


class _ServerSocket(WebSocket):
    """Server end of a test websocket: records what arrives."""

    def __init__(self, sock, on_message):
        super().__init__(sock)
        self._on_message = on_message

    def received_message(self, m):
        self._on_message(bytes(m.data), m.is_binary)


class FakeLXDWebsocketServer:
    """Accepts websocket connections the way LXD's operation endpoint does."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, ssl_context=None):
        self.ssl_context = ssl_context
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.requests = []  # (request line, {header: value})
        self.received = []  # (bytes, is_binary)
        self.sockets = []
        self.got_message = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _addr = self.listener.accept()
            except OSError:
                return
            try:
                if self.ssl_context:
                    conn = self.ssl_context.wrap_socket(conn, server_side=True)
                self._handshake(conn)
            except (OSError, ssl.SSLError):
                conn.close()

    def _handshake(self, conn):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(1024)
            if not chunk:
                raise OSError("client went away")
            request += chunk
        lines = request.split(b"\r\n\r\n")[0].decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        headers = {k.lower(): v for k, v in headers.items()}
        self.requests.append((lines[0], headers))
        accept = base64.b64encode(
            hashlib.sha1((headers["sec-websocket-key"] + self.GUID).encode()).digest()
        ).decode()
        ws = _ServerSocket(conn, self._record)
        ws.thread = threading.Thread(target=ws.run, daemon=True)
        self.sockets.append(ws)  # before the client can see the handshake end
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        ws.thread.start()

    def _record(self, data, binary):
        self.received.append((data, binary))
        self.got_message.set()

    def close(self):
        try:
            self.listener.shutdown(socket.SHUT_RDWR)  # wakes up accept()
        except OSError:
            pass
        self.listener.close()
        self.thread.join(5)
        for ws in self.sockets:
            shut_down(ws, ws.thread)


class ExecWebsocketTest(unittest.TestCase):
    """The real websocket client against a local websocket server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []
        self.got_output = threading.Event()
        self.closed = threading.Event()

    def start_server(self, ssl_context=None):
        server = FakeLXDWebsocketServer(ssl_context)
        self.addCleanup(server.close)
        return server

    def connect(self, url, ssl_options=None, pinned=None):
        def on_message(m):
            self.messages.append(bytes(m.data))
            self.got_output.set()

        ws = ind._ExecWebsocket(
            url,
            f"{OPERATION}/websocket?secret=data-secret",
            on_message,
            self.closed.set,
            ssl_options or {},
            pinned,
        )
        ws.connect()
        self.addCleanup(shut_down, ws, ws._th)
        return ws

    def tls_server(self):
        cert, key = make_certificate(self.tmp.name, "server")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return self.start_server(context), cert

    def test_frames_flow_both_ways_and_close_is_reported(self):
        server = self.start_server()
        ws = self.connect(f"ws://127.0.0.1:{server.port}")
        request_line, headers = server.requests[0]
        self.assertEqual(
            request_line, f"GET {OPERATION}/websocket?secret=data-secret HTTP/1.1"
        )
        self.assertNotIn("origin", headers)  # LXD would refuse the made-up one

        ws.send(b"echo hi\r", binary=True)
        self.assertTrue(server.got_message.wait(5))
        self.assertEqual(server.received, [(b"echo hi\r", True)])

        server.sockets[0].send(b"hi\r\n", binary=True)
        self.assertTrue(self.got_output.wait(5))
        self.assertEqual(self.messages, [b"hi\r\n"])

        server.sockets[0].close()
        self.assertTrue(self.closed.wait(5))

    def test_tls_with_matching_pinned_certificate_connects(self):
        server, cert = self.tls_server()
        ws = self.connect(
            f"wss://127.0.0.1:{server.port}", pinned=ind._pinned_fingerprint(cert)
        )
        ws.send(b"x", binary=True)
        self.assertTrue(server.got_message.wait(5))

    def test_tls_with_other_certificate_is_refused_before_the_secret_is_sent(self):
        server, _cert = self.tls_server()
        other, _key = make_certificate(self.tmp.name, "impostor")
        ws = ind._ExecWebsocket(
            f"wss://127.0.0.1:{server.port}",
            f"{OPERATION}/websocket?secret=data-secret",
            self.messages.append,
            self.closed.set,
            {},
            ind._pinned_fingerprint(other),
        )
        with self.assertRaisesRegex(ind.HandshakeError, "does not match"):
            ws.connect()
        self.assertIsNone(ws.sock)  # closed, not left dangling
        self.assertEqual(server.requests, [])

    def test_session_runs_over_real_websockets(self):
        server = self.start_server()
        client = make_client(f"ws://127.0.0.1:{server.port}")
        output, exits = [], []
        got_output, exited = threading.Event(), threading.Event()
        session = ind.TerminalSession(
            client,
            "web",
            on_output=lambda data: (output.append(data), got_output.set()),
            on_exit=lambda code: (exits.append(code), exited.set()),
        )
        session.start()
        self.addCleanup(
            lambda: [shut_down(ws, ws._th) for ws in (session._control, session._data)]
        )
        self.assertEqual(len(server.sockets), 2)
        control, data = server.sockets

        session.send_input(b"exit\r")
        self.assertTrue(server.got_message.wait(5))
        data.send(b"logout\r\n", binary=True)
        self.assertTrue(got_output.wait(5))
        data.close()
        self.assertTrue(exited.wait(5))
        self.assertEqual(server.received, [(b"exit\r", True)])
        self.assertEqual(output, [b"logout\r\n"])
        self.assertEqual(exits, [0])


class TerminalMenuTest(IndicatorTestCase):
    def terminal_items(self, instances):
        """Builds the menu; returns {label: sensitive} for the terminal items."""
        app = self.make_app(FakeServers({LOCAL: instances}))
        app._refresh_once()
        items = []

        def menu_item(label=None):
            item = mock.Mock(label=label)
            items.append(item)
            return item

        with mock.patch.object(ind.Gtk, "MenuItem", menu_item):
            app._build_or_update_menu()
        return [
            (i.label, i.set_sensitive.call_args.args[0])
            for i in items
            if i.label and "Terminal" in i.label
        ]

    INSTANCES = [
        FakeInstance("a-run"),
        FakeInstance("b-off", "Stopped"),
        FakeInstance("c-ice", "Frozen"),
        FakeInstance("d-busy", "Starting"),
    ]

    def test_terminal_item_follows_the_instance_status(self):
        self.assertEqual(
            self.terminal_items(self.INSTANCES),
            [
                ("Terminal", True),
                ("Start & Terminal", True),
                ("Terminal", False),
                ("Terminal", False),
            ],
        )

    def test_terminal_items_are_disabled_without_vte(self):
        with mock.patch.object(ind, "Vte", None):
            items = self.terminal_items(self.INSTANCES)
        self.assertEqual([sensitive for _, sensitive in items], [False] * 4)

    def test_start_and_terminal_starts_then_opens_a_terminal(self):
        db = FakeInstance("db", "Stopped")
        app = self.make_app(FakeServers({LOCAL: [db]}))
        app._refresh_once()
        with mock.patch.object(ind.GLib, "idle_add") as idle_add:
            app._start_then_open_terminal(None, "db")
        self.assertEqual(db.actions, ["start"])
        idle_add.assert_called_once_with(app._on_open_terminal, None, "db")

    def test_start_and_terminal_failed_start_opens_no_terminal(self):
        app = self.make_app(FakeServers({LOCAL: []}))
        app._refresh_once()
        app.notifier = mock.Mock()
        with mock.patch.object(ind.GLib, "idle_add", lambda f, *a: f(*a)):
            with mock.patch.object(ind, "TerminalWindow") as window_class:
                app._start_then_open_terminal(None, "gone")
        window_class.assert_not_called()
        app.notifier.action_failed.assert_called_once()

    def test_open_terminal_uses_that_remotes_client(self):
        app = self.make_app(
            FakeServers(
                {LOCAL: [FakeInstance("web")], "https://zeta:8443": [FakeInstance("a")]}
            )
        )
        app._refresh_once()
        with mock.patch.object(
            ind, "TerminalWindow", side_effect=lambda *a, **k: mock.Mock()
        ) as window_class:
            app._on_open_terminal("zeta", "a")
            app._on_open_terminal(None, "web")
        zeta_call, local_call = window_class.call_args_list
        self.assertIs(zeta_call.args[0], app.remote_clients["zeta"][1])
        self.assertEqual(zeta_call.args[1:], ("zeta", "a"))
        self.assertIs(local_call.args[0], app.lxd_client)
        self.assertEqual(len(app._terminal_windows), 2)

        # Closing a window lets it go.
        zeta_window = next(iter(app._terminal_windows))
        zeta_call.kwargs["on_destroy"](zeta_window)
        self.assertEqual(len(app._terminal_windows), 1)

    def test_open_terminal_without_connection_reports_error(self):
        app = self.make_app(FakeServers({}, unreachable={LOCAL}))
        app._refresh_once()
        app.notifier = mock.Mock()
        with mock.patch.object(ind, "TerminalWindow") as window_class:
            app._on_open_terminal("gone", "web")
        window_class.assert_not_called()
        app.notifier.action_failed.assert_called_once_with(
            "gone", "web", "Open Terminal", "Not connected."
        )


class TerminalWindowTest(unittest.TestCase):
    """The GTK side, with GTK, GLib and VTE mocked."""

    def setUp(self):
        patches = [
            mock.patch("builtins.print"),
            mock.patch.object(ind, "TerminalSession"),
            mock.patch.object(ind.threading, "Thread"),
            # Run idle callbacks right away.
            mock.patch.object(ind.GLib, "idle_add", side_effect=lambda f, *a: f(*a)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # The GTK and VTE mocks are shared by every test.
        ind.Gtk.reset_mock()
        ind.Vte.reset_mock()
        self.destroyed = []
        ind.Vte.Terminal.return_value.get_column_count.return_value = 80
        ind.Vte.Terminal.return_value.get_row_count.return_value = 24
        self.window = ind.TerminalWindow(
            mock.sentinel.client, "zeta", "web", on_destroy=self.destroyed.append
        )
        self.session = self.window.session
        self.terminal = self.window.terminal

    def callbacks(self):
        return ind.TerminalSession.call_args.kwargs

    def test_session_is_for_the_instance_and_terminal_size(self):
        args, kwargs = ind.TerminalSession.call_args
        self.assertEqual(args, (mock.sentinel.client, "web"))
        self.assertEqual((kwargs["columns"], kwargs["rows"]), (80, 24))
        ind.Gtk.Window.assert_called_with(title="zeta:web — LXD")
        ind.threading.Thread.assert_any_call(target=self.window._start, daemon=True)

    def test_typing_is_sent_as_utf8(self):
        self.window._on_commit(self.terminal, "é\r", 3)
        self.session.send_input.assert_called_with("é\r".encode())

    def test_size_changes_are_passed_on(self):
        self.terminal.get_column_count.return_value = 120
        self.terminal.get_row_count.return_value = 40
        self.window._on_size_allocate(self.terminal, None)
        self.session.resize.assert_called_with(120, 40)

    def test_output_is_fed_to_the_terminal(self):
        self.callbacks()["on_output"](b"hello")
        self.terminal.feed.assert_called_with(b"hello")

    def test_clean_exit_closes_the_window(self):
        self.callbacks()["on_exit"](0)
        self.window.window.destroy.assert_called_once()

    def test_failed_exit_closes_the_window(self):
        self.callbacks()["on_exit"](127)
        self.window.window.destroy.assert_called_once()

    def test_lost_connection_closes_the_window(self):
        self.callbacks()["on_exit"](None)
        self.window.window.destroy.assert_called_once()

    def test_start_failure_is_shown_in_the_terminal(self):
        self.session.start.side_effect = ind.LXDAPIException(
            mock.Mock(
                status_code=400, json=mock.Mock(return_value={"error": "not running"})
            )
        )
        self.window._start()
        self.assertIn(b"not running", self.terminal.feed.call_args.args[0])

    def test_closing_the_window_hangs_up(self):
        self.window._on_destroy(self.window.window)
        ind.threading.Thread.assert_called_with(target=self.session.close, daemon=True)
        self.assertEqual(self.destroyed, [self.window])
        self.terminal.feed.reset_mock()
        self.callbacks()["on_output"](b"late")
        self.callbacks()["on_exit"](0)
        self.terminal.feed.assert_not_called()
        self.window.window.destroy.assert_not_called()


class TerminalWindowLogoutTest(unittest.TestCase):
    """The window with a real TerminalSession on fake websockets."""

    def setUp(self):
        factory = self.factory = FakeWebsocketFactory()
        real_session = ind.TerminalSession
        patches = [
            mock.patch("builtins.print"),
            mock.patch.object(
                ind,
                "TerminalSession",
                lambda *a, **kw: real_session(*a, websocket_factory=factory, **kw),
            ),
            # Run the session's thread and idle callbacks right away.
            mock.patch.object(
                ind.threading,
                "Thread",
                lambda target, daemon: mock.Mock(start=target),
            ),
            mock.patch.object(ind.GLib, "idle_add", side_effect=lambda f, *a: f(*a)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        ind.Gtk.reset_mock()
        ind.Vte.reset_mock()
        ind.Vte.Terminal.return_value.get_column_count.return_value = 80
        ind.Vte.Terminal.return_value.get_row_count.return_value = 24
        self.client = make_client()
        self.window = ind.TerminalWindow(self.client, None, "web")
        # GTK is mocked, so pass the destroy signal on by hand.
        gtk_window = self.window.window
        gtk_window.destroy.side_effect = lambda: self.window._on_destroy(gtk_window)

    def exit_status(self, code):
        operation = self.client.api.operations.__getitem__.return_value
        operation.wait.get.return_value.json.return_value = {
            "metadata": {"metadata": {"return": code}}
        }

    def test_ctrl_d_logs_out_and_closes_the_window(self):
        # Ctrl+D makes the shell exit with the last command's status.
        self.exit_status(1)
        self.window._on_commit(self.window.terminal, "\x04", 1)
        self.assertIn((b"\x04", True), self.factory.data.sent)
        self.window.window.destroy.assert_not_called()

        self.factory.data.on_closed()  # the shell is gone: LXD closes the data
        self.window.window.destroy.assert_called_once()
        self.assertTrue(self.factory.control.closed)
        self.assertTrue(self.factory.data.closed)
        # The shell had already exited: no one to hang up.
        self.assertEqual(control_messages(self.factory.control), [])

    def test_dropped_connection_closes_the_window(self):
        operation = self.client.api.operations.__getitem__.return_value
        operation.wait.get.side_effect = ConnectionError("machine went away")
        self.factory.data.on_closed()
        self.window.window.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
