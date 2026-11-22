#!/usr/bin/env python3
"""System tray indicator showing the state of local and remote LXD instances."""

import concurrent.futures
import functools
import hashlib
import json
import os
import shutil
import signal
import ssl
import subprocess
import threading
import time
from urllib.parse import quote, urlparse

import gi

gi.require_version("Gtk", "3.0")
try:
    # Ubuntu 24.04+ ships the Ayatana fork; the old libappindicator is gone.
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
from gi.repository import Gio, GLib, Gtk

try:
    # The terminal widget behind GNOME Terminal; optional, for "Terminal".
    gi.require_version("Vte", "2.91")
    from gi.repository import Vte
except (ValueError, ImportError):
    Vte = None

import requests.certs
import yaml
from pylxd import Client
from pylxd.client import EventType
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound
from ws4py.client import WebSocketBaseClient
from ws4py.client.threadedclient import WebSocketClient
from ws4py.exc import HandshakeError

APPINDICATOR_ID = "lxd-status-indicator"
APP_NAME = "lxd-indicator"
REFRESH_INTERVAL = 10  # seconds
REMOTE_TIMEOUT = 5  # seconds; keeps an unreachable remote from stalling refreshes
EVENT_RETRY_INTERVAL = 5  # seconds between attempts to reopen the LXD event stream
# Changes seen this soon after launch only set the baseline: no notifications
# for everything that "appears" or settles while the indicator starts up.
STARTUP_QUIET_PERIOD = 15  # seconds
# Changes to the same instance this close together update one notification
# (e.g. "New Machine: foo" becomes "Created and started") instead of stacking.
NOTIFICATION_MERGE_WINDOW = 30  # seconds

# Freedesktop notification service (allowed by the snap's `desktop` plug).
NOTIFY_BUS_NAME = "org.freedesktop.Notifications"
NOTIFY_OBJECT_PATH = "/org/freedesktop/Notifications"
NOTIFY_IFACE = "org.freedesktop.Notifications"
# snapd installs the launcher as <snap>_<app>.desktop.
DESKTOP_ENTRY = (
    f"{os.environ['SNAP_NAME']}_{APP_NAME}" if os.environ.get("SNAP_NAME") else APP_NAME
)

SNAP_DIR = os.environ.get("SNAP")
# In the snap the icons sit at $SNAP; in a source checkout, next to this file.
ICON_DIR = SNAP_DIR or os.path.dirname(os.path.abspath(__file__))
# Panel icon: a symbolic icon, looked up by name in ICON_THEME_DIR. The panel
# recolors symbolic icons to match its own text, so the glyph stays visible on
# light and dark panels alike, whatever the application theme is.
ICON_THEME_DIR = os.path.join(ICON_DIR, "icons")
ICON_NORMAL = "lxd-indicator-symbolic"
ICON_NORMAL_FILE = os.path.join(
    ICON_THEME_DIR, "hicolor", "scalable", "apps", f"{ICON_NORMAL}.svg"
)
ICON_ALERT = os.path.join(ICON_DIR, "lxd_logo.svg")  # orange, full color

# Candidate LXD unix sockets, in order of preference. Inside a strict snap the
# `lxd` interface only grants access to the LXD snap's socket.
LXD_SOCKET_CANDIDATES = [
    "/var/snap/lxd/common/lxd/unix.socket",
    "/var/lib/lxd/unix.socket",
]

# The lxc client's configuration directory, which lists the remotes along with
# the client certificate and pinned server certificates, relative to the real
# home directory (inside a snap $HOME points at the snap's private data). The
# snap's `lxc-config` personal-files plug grants read access to these.
REAL_HOME = os.environ.get("SNAP_REAL_HOME") or os.path.expanduser("~")
LXC_CONFIG_CANDIDATES = [
    os.path.join(REAL_HOME, "snap", "lxd", "common", "config"),
    os.path.join(REAL_HOME, ".config", "lxc"),
]

AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "autostart"
)
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, f"{APP_NAME}.desktop")

# The shell started by "Terminal": a login shell for root, as `lxc shell`
# starts (its alias is `exec @ARGS@ -- su -l`); su keeps TERM. Images
# without su get a plain login sh instead.
TERMINAL_COMMAND = [
    "sh",
    "-c",
    "if command -v su >/dev/null 2>&1; then exec su -l; fi; exec sh -l",
]
TERMINAL_TERM = "xterm-256color"  # what VTE emulates
TERMINAL_SCROLLBACK = 10000  # lines
SIGHUP = 1

# Freedesktop icon names for instance states
ICON_RUNNING = "media-playback-start-symbolic"
ICON_STOPPED = "media-playback-stop-symbolic"
ICON_PAUSED = "media-playback-pause-symbolic"  # "Frozen" state
ICON_PENDING = "view-refresh-symbolic"  # transitional / unknown states


def _lxd_socket_path():
    if "LXD_DIR" in os.environ:
        return os.path.join(os.environ["LXD_DIR"], "unix.socket")
    for path in LXD_SOCKET_CANDIDATES:
        if os.path.exists(path):
            return path
    return LXD_SOCKET_CANDIDATES[0]


def _snap_plug_connected(plug):
    """Returns True/False for a snap plug's connection state, None if unknown."""
    if not SNAP_DIR:
        return None
    try:
        result = subprocess.run(
            ["snapctl", "is-connected", plug], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def _diagnose_connection_failure(socket_path, error):
    """Turns a low-level connection failure into an actionable message."""
    if _snap_plug_connected("lxd") is False:
        return (
            "LXD interface not connected. Run: "
            "sudo snap connect lxd-indicator:lxd lxd"
        )
    if not os.path.exists(socket_path):
        return f"LXD socket not found at {socket_path}. Is LXD installed and running?"
    if "Permission denied" in str(error) or not os.access(
        socket_path, os.R_OK | os.W_OK
    ):
        return (
            "Permission denied on the LXD socket. Add yourself to the 'lxd' group "
            "(sudo usermod -aG lxd $USER) and log out and back in."
        )
    return f"LXD connection failed: {error}"


def _lxc_config_dir():
    if "LXD_CONF" in os.environ:
        return os.environ["LXD_CONF"]
    for path in LXC_CONFIG_CANDIDATES:
        if os.path.exists(os.path.join(path, "config.yml")):
            return path
    return None


def _load_remotes():
    """Returns (config_dir, {name: remote}) for remotes that can host instances.

    Raises OSError or yaml.YAMLError if the config exists but can't be read.
    """
    config_dir = _lxc_config_dir()
    if not config_dir:
        return None, {}
    config_file = os.path.join(config_dir, "config.yml")
    if not os.path.exists(config_file):
        return config_dir, {}
    with open(config_file) as f:
        config = yaml.safe_load(f) or {}

    remotes = {}
    for name, remote in (config.get("remotes") or {}).items():
        if not isinstance(remote, dict) or name == "local":
            continue
        addr = remote.get("addr") or ""
        # Image servers (simplestreams, public remotes) have no instances, and
        # unix:// remotes are the local daemon, which is listed already.
        if (
            remote.get("protocol", "lxd") != "lxd"
            or remote.get("public")
            or not addr.startswith("https://")
        ):
            continue
        if remote.get("auth_type") == "oidc":
            print(f"Skipping remote {name}: OIDC authentication is not supported.")
            continue
        remotes[name] = remote
    return config_dir, remotes


def _instance_dicts(remote, client):
    return [
        {
            "remote": remote,
            "name": item.name,
            "status": item.status.lower(),
            "type": getattr(item, "type", "container").lower(),
        }
        for item in client.instances.all()
    ]


def _instance_label(remote, name):
    return name if remote is None else f"{remote}:{name}"


def _can_open_terminal(status):
    """Whether "Terminal" works for an instance: it needs a running one."""
    return Vte is not None and status == "running"


def _sort_key(info):
    # Local instances first, then each remote in alphabetical order.
    return (info["remote"] is not None, info["remote"] or "", info["name"])


def _instance_changes(previous, current):
    """Returns [(name, [change, ...])] between two {name: status} snapshots.

    Changes are "created", "started", "stopped" and "deleted". Thawing a
    frozen instance isn't reported as a start.
    """
    changes = []
    for name in sorted(set(previous) | set(current)):
        old, status = previous.get(name), current.get(name)
        found = []
        if status is None:
            found.append("deleted")
        elif old is None:
            found.append("created")
        if status == "running" and old not in ("running", "frozen"):
            found.append("started")
        elif status == "stopped" and old not in (None, "stopped"):
            found.append("stopped")
        if found:
            changes.append((name, found))
    return changes


_CHANGE_TITLES = {
    "created": "Created",
    "started": "Started",
    "stopped": "Stopped",
    "deleted": "Deleted",
}
_ACTION_TITLES = {"start": "Start", "stop": "Stop"}


def _notification_text(label, changes):
    """Returns (summary, body) describing an instance's recent changes.

    The summary says where the instance ended up ("New Machine" while it
    exists after being created); the body lists the changes when there were
    several.
    """
    last = changes[-1]
    if last != "deleted" and "created" in changes:
        summary = f"New Machine: {label}"
    else:
        summary = f"{_CHANGE_TITLES[last]}: {label}"
    if len(changes) == 1:
        return summary, ""
    words = [_CHANGE_TITLES[changes[0]]] + changes[1:]
    if changes[0] == "created":
        return summary, ", ".join(words[:-1]) + " and " + words[-1]
    return summary, ", then ".join(words)


class Notifier:
    """Desktop notifications that are updated in place while events keep coming.

    Each instance (and each failed action) keeps the ID the notification server
    gave its notification, so later changes replace that notification rather
    than adding another. Runs on the GTK main thread.
    """

    def __init__(self):
        self._proxy = None
        # key -> {"id", "changes", "time", "summary", "body", "busy", "dirty"}
        self._entries = {}
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                NOTIFY_BUS_NAME,
                NOTIFY_OBJECT_PATH,
                NOTIFY_IFACE,
                None,
            )
            self._proxy.connect("g-signal", self._on_signal)
        except GLib.Error as e:
            self._proxy = None
            print(f"Notification service unavailable: {e.message}")

    def instance_changes(self, remote, name, changes):
        key = ("instance", remote, name)
        entry = self._entry(key)
        for change in changes:
            if not entry["changes"] or entry["changes"][-1] != change:
                entry["changes"].append(change)
        summary, body = _notification_text(
            _instance_label(remote, name), entry["changes"]
        )
        self._show(key, summary, body)
        return False  # one-shot when used with GLib.idle_add

    def action_failed(self, remote, name, action, error):
        key = ("failure", remote, name, action)
        self._entry(key)
        summary = f"Failed to {_ACTION_TITLES.get(action, action.title())}"
        self._show(key, summary, f"{_instance_label(remote, name)}: {error}")
        return False

    def _entry(self, key):
        """Returns the key's entry, starting afresh once its last update is old."""
        now = time.monotonic()
        for old_key, old in list(self._entries.items()):
            if now - old["time"] > NOTIFICATION_MERGE_WINDOW and not old["busy"]:
                del self._entries[old_key]
        entry = self._entries.setdefault(
            key, {"id": 0, "changes": [], "busy": False, "dirty": False}
        )
        entry["time"] = now
        return entry

    def _show(self, key, summary, body):
        entry = self._entries[key]
        entry["summary"], entry["body"] = summary, body
        print(f"Notification: {summary}" + (f" ({body})" if body else ""))
        if entry["busy"]:
            # The server hasn't told us the ID yet; resend once it has.
            entry["dirty"] = True
            return
        entry["busy"] = True
        args = (
            APP_NAME,
            entry["id"],
            ICON_ALERT,
            summary,
            body,
            [],
            {"desktop-entry": DESKTOP_ENTRY},
            -1,
        )
        self._dbus_notify(args, functools.partial(self._on_notified, key, entry))

    def _on_notified(self, key, entry, notification_id):
        entry["busy"] = False
        if notification_id:
            entry["id"] = notification_id
        if entry["dirty"] and self._entries.get(key) is entry:
            entry["dirty"] = False
            self._show(key, entry["summary"], entry["body"])

    def _dbus_notify(self, args, done):
        """Calls Notify; done(id) runs with the new ID, or 0 if it failed."""
        if self._proxy is None:
            done(0)
            return

        def finish(proxy, result):
            try:
                done(proxy.call_finish(result).unpack()[0])
            except GLib.Error as e:
                print(f"Failed to show notification: {e.message}")
                done(0)

        self._proxy.call(
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                args[:6]
                + ({k: GLib.Variant("s", v) for k, v in args[6].items()},)
                + args[7:],
            ),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            finish,
        )

    def _on_signal(self, _proxy, _sender, signal_name, parameters):
        if signal_name != "NotificationClosed":
            return
        closed_id = parameters.unpack()[0]
        # Once dismissed, the next change gets a fresh notification.
        for key, entry in list(self._entries.items()):
            if entry["id"] == closed_id and not entry["busy"]:
                del self._entries[key]


class _LifecycleEventClient(WebSocketBaseClient):
    """Websocket client for LXD's /1.0/events that hands each event to a callback."""

    on_event = None

    def __init__(self, url, **kwargs):
        # LXD rejects ws4py's made-up Origin header on unix sockets.
        kwargs["exclude_headers"] = list(kwargs.get("exclude_headers") or []) + [
            "Origin"
        ]
        super().__init__(url, **kwargs)

    def handshake_ok(self):
        pass

    def received_message(self, message):
        try:
            event = json.loads(message.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if self.on_event:
            self.on_event(event)


def _pinned_fingerprint(path):
    """Returns the SHA-256 fingerprint of the PEM certificate at path."""
    with open(path) as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def _websocket_security(client):
    """Returns (ssl_options, pinned fingerprint or None) for client's websockets.

    The websockets must be as trusted as the client's HTTPS connection: the
    same client certificate, and the server checked against the certificate
    lxc pinned for the remote, or else against the system CAs.
    """
    if not client.websocket_url.startswith("wss:"):
        return {}, None
    options = {}
    if client.cert:
        options["certfile"], options["keyfile"] = client.cert
    verify = client.api.session.verify
    if isinstance(verify, str):
        # Pinned certificates are self-signed and seldom name the address
        # used, so they're compared by fingerprint once connected instead.
        return options, _pinned_fingerprint(verify)
    if verify is not False:
        options["ca_certs"] = requests.certs.where()
        options["cert_reqs"] = ssl.CERT_REQUIRED
    return options, None


class _ExecWebsocket(WebSocketClient):
    """One websocket of an exec operation, reporting to callbacks."""

    def __init__(self, url, resource, on_message, on_closed, ssl_options, pinned):
        # Like pylxd: LXD rejects the Origin ws4py makes up for unix sockets.
        super().__init__(url, ssl_options=ssl_options, exclude_headers=["origin"])
        self.resource = resource
        self._on_message = on_message
        self._on_closed = on_closed
        self._pinned = pinned

    @property
    def handshake_request(self):
        # Built once TLS is up and before anything, the secret included, is
        # sent: the place to check the server against the pinned certificate.
        if self._pinned is not None:
            der = self.sock.getpeercert(binary_form=True)
            if der is None or hashlib.sha256(der).hexdigest() != self._pinned:
                self.close_connection()
                raise HandshakeError("server certificate does not match lxc's")
        return super().handshake_request

    def received_message(self, message):
        self._on_message(message)

    def closed(self, code, reason=None):
        self._on_closed()


class TerminalSession:
    """An interactive shell in an instance, like `lxc shell <instance>`.

    Runs the command through the LXD exec API, so it needs only the LXD
    connection the indicator already has. The terminal's input goes in with
    send_input() and resize(); output comes back through on_output(bytes).
    Once the shell exits or the connection drops, on_exit(exit code or None)
    is called, once. Callbacks run on the websocket threads.
    """

    def __init__(
        self,
        client,
        instance_name,
        on_output,
        on_exit,
        columns=80,
        rows=24,
        command=None,
        websocket_factory=_ExecWebsocket,
    ):
        self.client = client
        self.instance_name = instance_name
        self.on_output = on_output
        self.on_exit = on_exit
        self.command = command or TERMINAL_COMMAND
        self.websocket_factory = websocket_factory
        self.operation_id = None
        self._size = (columns, rows)
        self._sent_size = None  # the size LXD was last told
        self._data = None
        self._control = None
        self._pending = []  # input typed before the session is connected
        self._closing = False
        self._exited = False
        self._lock = threading.Lock()

    def start(self):
        """Starts the shell and connects to it. Blocks; raises on failure."""
        api = self.client.api
        with self._lock:
            columns, rows = self._sent_size = self._size
        response = api.instances[self.instance_name]["exec"].post(
            json={
                "command": self.command,
                "environment": {"TERM": TERMINAL_TERM},
                "interactive": True,
                "wait-for-websocket": True,
                "width": columns,
                "height": rows,
            }
        )
        body = response.json()
        fds = body["metadata"]["metadata"]["fds"]
        self.operation_id = body["operation"].split("/")[-1].split("?")[0]
        path = urlparse(api.operations[self.operation_id].websocket._api_endpoint).path
        url = self.client.websocket_url
        ssl_options, pinned = _websocket_security(self.client)

        def connect(fd, on_message, on_closed):
            ws = self.websocket_factory(
                url,
                f"{path}?secret={fds[fd]}",
                on_message,
                on_closed,
                ssl_options,
                pinned,
            )
            ws.connect()
            return ws

        try:
            control = connect("control", lambda _message: None, lambda: None)
            with self._lock:
                self._control = control
            # LXD starts the command once every websocket is connected.
            data = connect("0", self._on_data, self._on_data_closed)
        except Exception:
            self.close()
            raise
        with self._lock:
            self._data = data
            pending, self._pending = self._pending, []
            closing = self._closing
        if closing:  # the window went away while connecting
            self.close()
            return
        for chunk in pending:
            self._send(data, chunk, binary=True)
        self._send_size()

    def send_input(self, data):
        """Sends keystrokes (bytes) to the shell."""
        with self._lock:
            data_ws = self._data
            if data_ws is None:
                if not self._closing:
                    self._pending.append(data)
                return
        self._send(data_ws, data, binary=True)

    def resize(self, columns, rows):
        """Tells the shell the terminal is now columns x rows."""
        with self._lock:
            self._size = (columns, rows)
        self._send_size()

    def close(self):
        """Hangs up the shell and disconnects."""
        with self._lock:
            self._closing = True
            control, data = self._control, self._data
            exited = self._exited
        if control is not None and not exited:  # else there's no one to hang up
            self._send(control, self._control_message("signal", signal=SIGHUP))
        for ws in (data, control):
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # already gone
                    pass

    @staticmethod
    def _control_message(command, **fields):
        return json.dumps({"command": command, **fields})

    def _send_size(self):
        with self._lock:
            control = self._control if self._data is not None else None
            if control is None or self._size == self._sent_size:
                return
            columns, rows = self._sent_size = self._size
        args = {"width": str(columns), "height": str(rows)}
        self._send(control, self._control_message("window-resize", args=args))

    @staticmethod
    def _send(ws, payload, binary=False):
        try:
            ws.send(payload, binary=binary)
        except Exception as e:  # the connection is going; closed() follows
            print(f"Terminal: failed to send: {e}")

    def _on_data(self, message):
        if message.data:
            self.on_output(bytes(message.data))

    def _on_data_closed(self):
        with self._lock:
            if self._exited:
                return
            self._exited = True
            control = self._control
        if control is not None:
            try:
                control.close()
            except Exception:
                pass
        self.on_exit(self._exit_code())

    def _exit_code(self):
        """The shell's exit status, or None if it's unknown (connection lost)."""
        if self.operation_id is None:
            return None
        try:
            # The command has ended by now, or is about to: wait briefly.
            response = self.client.api.operations[self.operation_id].wait.get(
                params={"timeout": 3}
            )
            return response.json()["metadata"]["metadata"]["return"]
        except Exception as e:
            print(f"Terminal: no exit status for {self.instance_name}: {e}")
            return None


class TerminalWindow:
    """A window with a terminal running a shell in an instance."""

    def __init__(self, client, remote, instance_name, on_destroy=None):
        self.label = _instance_label(remote, instance_name)
        self._on_destroy_callback = on_destroy
        self._destroyed = False

        self.window = Gtk.Window(title=f"{self.label} — LXD")
        self.window.set_default_size(800, 500)
        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(TERMINAL_SCROLLBACK)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.terminal)
        self.window.add(scrolled)

        self.session = TerminalSession(
            client,
            instance_name,
            on_output=lambda data: GLib.idle_add(self._feed, data),
            on_exit=lambda code: GLib.idle_add(self._on_exit, code),
            columns=self.terminal.get_column_count(),
            rows=self.terminal.get_row_count(),
        )
        self.terminal.connect("commit", self._on_commit)
        self.terminal.connect("size-allocate", self._on_size_allocate)
        self.window.connect("destroy", self._on_destroy)
        self.window.show_all()
        self.terminal.grab_focus()

        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        try:
            self.session.start()
        except Exception as e:
            print(f"Terminal for {self.label} failed: {e}")
            GLib.idle_add(self._show_message, f"Couldn't start a shell: {e}")

    def _feed(self, data):
        if not self._destroyed:
            self.terminal.feed(data)
        return False

    def _show_message(self, text):
        self._feed(f"\r\n[{text}]\r\n".encode())
        return False

    def _on_exit(self, code):
        if self._destroyed:
            return False
        if code == 0:
            # Like a terminal app, close once the shell exits normally.
            self.window.destroy()
        elif code is None:
            self._show_message("Connection to the instance closed")
        else:
            self._show_message(f"Shell exited with status {code}")
        return False

    def _on_commit(self, _terminal, text, _size):
        self.session.send_input(text.encode("utf-8", "surrogateescape"))

    def _on_size_allocate(self, terminal, _allocation):
        self.session.resize(terminal.get_column_count(), terminal.get_row_count())

    def _on_destroy(self, _window):
        self._destroyed = True
        threading.Thread(target=self.session.close, daemon=True).start()
        if self._on_destroy_callback:
            self._on_destroy_callback(self)


class LXDIndicatorApp:
    def __init__(self):
        self.lxd_client = None
        self.lxd_error_message = None
        # Remote name -> (connection settings, pylxd Client) for lxc remotes.
        self.remote_clients = {}
        # Remote name -> error message for remotes that failed this refresh.
        self.remote_errors = {}
        self.lxd_instances_cache = []
        self._lock = threading.Lock()
        self._refresh_event = threading.Event()
        # Source (None for local, else remote name) -> {instance name: status}
        # from its last successful listing, to spot changes for notifications.
        self._known_instances = {}
        self._quiet_until = time.monotonic() + STARTUP_QUIET_PERIOD
        self._terminal_windows = set()  # open TerminalWindows, kept alive

        self._current_icon = None

        self.indicator = AppIndicator3.Indicator.new(
            APPINDICATOR_ID,
            ICON_NORMAL,
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        # The panel, not this process, loads the icon: tell it where to look.
        self.indicator.set_icon_theme_path(ICON_THEME_DIR)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("LXD Indicator")
        self._update_icon()

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._build_or_update_menu()
        self.notifier = Notifier()

        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.refresh_thread.start()
        self.events_thread = threading.Thread(target=self._event_loop, daemon=True)
        self.events_thread.start()

    # ---- LXD access (worker thread) -------------------------------------

    def _connect_lxd(self):
        socket_path = _lxd_socket_path()
        try:
            self.lxd_client = Client(
                endpoint=f"http+unix://{quote(socket_path, safe='')}"
            )
            self.lxd_error_message = None
            print(f"Successfully connected to LXD at {socket_path}.")
        except ClientConnectionFailed as e:
            self.lxd_client = None
            self.lxd_error_message = _diagnose_connection_failure(socket_path, e)
            print(f"Error: {self.lxd_error_message} ({e})")
        except LXDAPIException as e:
            self.lxd_client = None
            self.lxd_error_message = f"LXD API error during connection: {e}"
            print(f"Error: {self.lxd_error_message}")

    def _fetch_instances(self):
        """Returns a list of instance dicts; sets lxd_error_message on failure."""
        if not self.lxd_client:
            self._connect_lxd()
            if not self.lxd_client:
                return []
        try:
            instances = _instance_dicts(None, self.lxd_client)
            self.lxd_error_message = None
            return instances
        except LXDAPIException as e:
            self.lxd_error_message = f"LXD API error: {e}"
        except Exception as e:  # requests ConnectionError etc. — reconnect next time
            self.lxd_client = None
            self.lxd_error_message = f"Lost connection to LXD: {e}"
        print(f"Error during fetch: {self.lxd_error_message}")
        return []

    def _remote_client(self, config_dir, name, remote):
        """Returns a connected client for an lxc remote, reusing a cached one."""
        settings = (config_dir, remote.get("addr"), remote.get("project"))
        cached = self.remote_clients.get(name)
        if cached and cached[0] == settings:
            return cached[1]

        cert = (
            os.path.join(config_dir, "client.crt"),
            os.path.join(config_dir, "client.key"),
        )
        if not all(os.path.exists(path) for path in cert):
            cert = None
        # lxc pins each remote's certificate here when the remote is added;
        # pylxd checks the server against that fingerprint when given a path.
        server_cert = os.path.join(config_dir, "servercerts", f"{name}.crt")
        verify = server_cert if os.path.exists(server_cert) else True

        client = Client(
            endpoint=remote["addr"],
            cert=cert,
            verify=verify,
            timeout=REMOTE_TIMEOUT,
            project=remote.get("project"),
        )
        self.remote_clients[name] = (settings, client)
        print(f"Successfully connected to remote {name} at {remote['addr']}.")
        return client

    def _fetch_remote_instances(self, config_dir, name, remote):
        """Returns (instances, error message or None) for one lxc remote."""
        try:
            client = self._remote_client(config_dir, name, remote)
            return _instance_dicts(name, client), None
        except ClientConnectionFailed as e:
            error = f"Connection failed: {e}" if str(e) else "Connection failed"
        except LXDAPIException as e:
            error = f"LXD API error: {e}"
        except Exception as e:  # requests errors etc.
            error = f"Lost connection: {e}"
        # Reconnect from scratch next time.
        self.remote_clients.pop(name, None)
        print(f"Error fetching instances from remote {name}: {error}")
        return [], error

    def _fetch_all_remote_instances(self):
        """Returns (instances, {remote: error}, configured remote names)."""
        try:
            config_dir, remotes = _load_remotes()
        except (OSError, yaml.YAMLError) as e:
            if _snap_plug_connected("lxc-config") is False:
                message = (
                    "Can't read the lxc config. Run: "
                    "sudo snap connect lxd-indicator:lxc-config"
                )
            else:
                message = f"Can't read the lxc config: {e}"
            print(f"Error: {message}")
            # The remotes are unknown, not removed: keep what's known of them.
            return [], {"remotes": message}, None

        # Forget clients for remotes that were removed from the config.
        for name in set(self.remote_clients) - set(remotes):
            del self.remote_clients[name]
        if not remotes:
            return [], {}, set()

        instances, errors = [], {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(remotes)) as pool:
            futures = {
                name: pool.submit(
                    self._fetch_remote_instances, config_dir, name, remote
                )
                for name, remote in remotes.items()
            }
            for name, future in futures.items():
                remote_instances, error = future.result()
                instances.extend(remote_instances)
                if error:
                    errors[name] = error
        return instances, errors, set(remotes)

    def _refresh_once(self):
        with self._lock:
            previous = (
                self.lxd_instances_cache,
                self.lxd_error_message,
                self.remote_errors,
            )
            local_instances = self._fetch_instances()
            local_ok = self.lxd_error_message is None
            remote_instances, self.remote_errors, remote_names = (
                self._fetch_all_remote_instances()
            )
            self.lxd_instances_cache = sorted(
                local_instances + remote_instances, key=_sort_key
            )
            current = (
                self.lxd_instances_cache,
                self.lxd_error_message,
                self.remote_errors,
            )
            if current != previous:
                GLib.idle_add(self._build_or_update_menu)

            listed = set(remote_names or ()) - set(self.remote_errors)
            if local_ok:
                listed.add(None)
            self._track_changes(listed, remote_names)

    def _track_changes(self, listed, remote_names):
        """Notifies about instance changes in the sources listed this refresh.

        A source's first successful listing only records a baseline, as does
        everything during the startup quiet period. Sources that failed keep
        their old baseline, so changes made while unreachable are still
        reported once they're back.
        """
        if remote_names is not None:
            for source in list(self._known_instances):
                if source is not None and source not in remote_names:
                    del self._known_instances[source]
        quiet = time.monotonic() < self._quiet_until
        for source in listed:
            snapshot = {
                info["name"]: info["status"]
                for info in self.lxd_instances_cache
                if info["remote"] == source
            }
            previous = self._known_instances.get(source)
            self._known_instances[source] = snapshot
            if previous is None or quiet:
                continue
            for name, changes in _instance_changes(previous, snapshot):
                GLib.idle_add(self.notifier.instance_changes, source, name, changes)

    def _refresh_loop(self):
        while True:
            # Clear first, so a request made during the refresh isn't lost.
            self._refresh_event.clear()
            self._refresh_once()
            self._refresh_event.wait(REFRESH_INTERVAL)

    def _on_lxd_event(self, event):
        action = (event.get("metadata") or {}).get("action") or ""
        if action.startswith("instance-"):
            print(f"LXD event: {action}")
            self._refresh_event.set()

    def _watch_events_once(self):
        """Follows the local daemon's lifecycle events until the stream drops."""
        client = self.lxd_client
        if client is None:
            return
        ws = client.events(
            websocket_client=_LifecycleEventClient,
            event_types={EventType.Lifecycle},
        )
        ws.on_event = self._on_lxd_event
        ws.connect()
        print("Watching LXD events.")
        # Catch anything that changed while the stream was down.
        self._refresh_event.set()
        ws.run()  # closes the socket when it returns

    def _event_loop(self):
        # Instances started, stopped or created by `lxc` or anything else show
        # up as lifecycle events; each one triggers an immediate refresh, and
        # the refresh works out what to notify about.
        while True:
            try:
                self._watch_events_once()
            except Exception as e:
                print(f"LXD event stream unavailable: {e}")
            time.sleep(EVENT_RETRY_INTERVAL)

    def _client_for(self, remote):
        """The connected client for remote (None for local), or None."""
        if remote is None:
            return self.lxd_client
        return self.remote_clients.get(remote, (None, None))[1]

    def _run_action(self, remote, instance_name, action):
        label = _instance_label(remote, instance_name)
        with self._lock:
            try:
                client = self._client_for(remote)
                if not client:
                    raise RuntimeError("LXD client not available")
                instance = client.instances.get(instance_name)
                getattr(instance, action)(wait=True, timeout=30)
                print(f"{action} completed on {label}.")
            except NotFound:
                error = "Instance not found."
                self.lxd_error_message = f"Instance {label} not found."
            except Exception as e:
                error = str(e) or type(e).__name__
                self.lxd_error_message = f"Failed to {action} {label}: {e}"
            else:
                # Refresh right away so the new state shows up. On failure, leave
                # the error visible until the next periodic refresh instead.
                self._refresh_event.set()
                return
            print(f"Error: {self.lxd_error_message}")
            GLib.idle_add(
                self.notifier.action_failed,
                remote,
                instance_name,
                action,
                error,
            )
            GLib.idle_add(self._build_or_update_menu)

    # ---- UI (GTK main thread) -------------------------------------------

    def _update_icon(self):
        # Alert when LXD can't be reached: no client and a reason to show.
        if self.lxd_client is None and self.lxd_error_message:
            icon, description = ICON_ALERT, "LXD unavailable"
        else:
            icon, description = ICON_NORMAL, "LXD"
        if icon != self._current_icon:
            self._current_icon = icon
            self.indicator.set_icon_full(icon, description)

    def _build_or_update_menu(self):
        self._update_icon()

        for item in self.menu.get_children():
            self.menu.remove(item)

        errors = [self.lxd_error_message] if self.lxd_error_message else []
        errors += [
            f"{remote}: {message}"
            for remote, message in sorted(self.remote_errors.items())
        ]
        for message in errors:
            error_item = Gtk.MenuItem(label=message)
            error_item.set_sensitive(False)
            self.menu.append(error_item)
        if errors:
            self.menu.append(Gtk.SeparatorMenuItem())

        instances = list(self.lxd_instances_cache)
        for info in instances:
            remote, name, status = info["remote"], info["name"], info["status"]

            item = Gtk.MenuItem()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon_name = {
                "running": ICON_RUNNING,
                "stopped": ICON_STOPPED,
                "frozen": ICON_PAUSED,
            }.get(status, ICON_PENDING)
            box.pack_start(
                Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU),
                False,
                False,
                0,
            )
            box.pack_start(
                Gtk.Label(label=_instance_label(remote, name), xalign=0), True, True, 0
            )
            item.add(box)

            submenu = Gtk.Menu()
            start = Gtk.MenuItem(label="Start")
            start.connect(
                "activate",
                functools.partial(self._on_instance_action, remote, name, "start"),
            )
            start.set_sensitive(status not in ("running", "frozen"))
            stop = Gtk.MenuItem(label="Stop")
            stop.connect(
                "activate",
                functools.partial(self._on_instance_action, remote, name, "stop"),
            )
            stop.set_sensitive(status in ("running", "frozen"))
            submenu.append(start)
            submenu.append(stop)
            submenu.append(Gtk.SeparatorMenuItem())
            terminal = Gtk.MenuItem(label="Terminal")
            terminal.connect(
                "activate",
                functools.partial(self._on_open_terminal, remote, name),
            )
            terminal.set_sensitive(_can_open_terminal(status))
            if Vte is None:
                terminal.set_tooltip_text(
                    "Needs the VTE terminal widget (gir1.2-vte-2.91)"
                )
            submenu.append(terminal)
            item.set_submenu(submenu)
            self.menu.append(item)

        if instances:
            self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh Now")
        refresh_item.connect("activate", lambda _w: self._refresh_event.set())
        self.menu.append(refresh_item)

        autostart_item = Gtk.CheckMenuItem(label="Start at Login")
        autostart_item.set_active(os.path.exists(AUTOSTART_FILE))
        autostart_item.connect("toggled", self._on_autostart_toggled)
        self.menu.append(autostart_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()
        return False  # one-shot when used with GLib.idle_add

    def _on_instance_action(self, remote, instance_name, action, _widget):
        print(f"Action: {action} on instance {_instance_label(remote, instance_name)}")
        threading.Thread(
            target=self._run_action, args=(remote, instance_name, action), daemon=True
        ).start()

    def _on_open_terminal(self, remote, instance_name, _widget):
        label = _instance_label(remote, instance_name)
        client = self._client_for(remote)
        if client is None:
            self.lxd_error_message = f"Can't open a terminal on {label}: not connected."
            self._build_or_update_menu()
            return
        print(f"Opening a terminal on {label}")
        window = TerminalWindow(
            client, remote, instance_name, on_destroy=self._terminal_windows.discard
        )
        self._terminal_windows.add(window)

    def _on_autostart_toggled(self, widget):
        # For snaps, snapd's `autostart:` support launches the app at login when
        # this file exists in $SNAP_USER_DATA/.config/autostart.
        try:
            if widget.get_active():
                source = os.path.join(
                    SNAP_DIR or "", "meta", "gui", f"{APP_NAME}.desktop"
                )
                if not os.path.exists(source):
                    source = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "snap",
                        "gui",
                        f"{APP_NAME}.desktop",
                    )
                os.makedirs(AUTOSTART_DIR, exist_ok=True)
                shutil.copyfile(source, AUTOSTART_FILE)
            elif os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
        except OSError as e:
            print(f"Failed to update autostart: {e}")


if __name__ == "__main__":
    for icon_path in (ICON_NORMAL_FILE, ICON_ALERT):
        if not os.path.exists(icon_path):
            print(f"Warning: icon not found at {icon_path}")
    if Vte is None:
        print("VTE not available: the Terminal menu items are disabled.")
    else:
        version = f"{Vte.get_major_version()}.{Vte.get_minor_version()}"
        print(f"Terminal support enabled (VTE {version}).")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = LXDIndicatorApp()
    Gtk.main()
