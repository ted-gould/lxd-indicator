#!/usr/bin/env python3
"""System tray indicator showing the state of local and remote LXD instances."""

import concurrent.futures
import functools
import os
import shutil
import signal
import subprocess
import threading
from urllib.parse import quote

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

import yaml
from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

APPINDICATOR_ID = "lxd-status-indicator"
APP_NAME = "lxd-indicator"
REFRESH_INTERVAL = 10  # seconds
REMOTE_TIMEOUT = 5  # seconds; keeps an unreachable remote from stalling refreshes

SNAP_DIR = os.environ.get("SNAP")
# In the snap the icons sit at $SNAP; in a source checkout, next to this file.
ICON_DIR = SNAP_DIR or os.path.dirname(os.path.abspath(__file__))
# Panel icons: monochrome to match the desktop theme, orange for alerts.
ICON_LIGHT_THEME = os.path.join(ICON_DIR, "lxd_logo_light.svg")  # white with black
ICON_DARK_THEME = os.path.join(ICON_DIR, "lxd_logo_dark.svg")  # black with white
ICON_ALERT = os.path.join(ICON_DIR, "lxd_logo.svg")  # orange with white

# XDG desktop portal, which reports the user's light/dark preference.
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SETTINGS_IFACE = "org.freedesktop.portal.Settings"
APPEARANCE_NAMESPACE = "org.freedesktop.appearance"
COLOR_SCHEME_KEY = "color-scheme"  # 0 = no preference, 1 = dark, 2 = light

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


def _sort_key(info):
    # Local instances first, then each remote in alphabetical order.
    return (info["remote"] is not None, info["remote"] or "", info["name"])


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

        self._current_icon = None
        self._portal = None
        self._portal_color_scheme = 0

        self.indicator = AppIndicator3.Indicator.new(
            APPINDICATOR_ID,
            ICON_LIGHT_THEME,
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("LXD Indicator")
        self._watch_theme()
        self._update_icon()

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._build_or_update_menu()

        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.refresh_thread.start()

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
        """Returns (instances, {remote: error}) across all configured remotes."""
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
            return [], {"remotes": message}

        # Forget clients for remotes that were removed from the config.
        for name in set(self.remote_clients) - set(remotes):
            del self.remote_clients[name]
        if not remotes:
            return [], {}

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
        return instances, errors

    def _refresh_once(self):
        with self._lock:
            previous = (
                self.lxd_instances_cache,
                self.lxd_error_message,
                self.remote_errors,
            )
            local_instances = self._fetch_instances()
            remote_instances, self.remote_errors = self._fetch_all_remote_instances()
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

    def _refresh_loop(self):
        while True:
            self._refresh_once()
            self._refresh_event.wait(REFRESH_INTERVAL)
            self._refresh_event.clear()

    def _run_action(self, remote, instance_name, action):
        label = _instance_label(remote, instance_name)
        with self._lock:
            try:
                if remote is None:
                    client = self.lxd_client
                else:
                    client = self.remote_clients.get(remote, (None, None))[1]
                if not client:
                    raise RuntimeError("LXD client not available")
                instance = client.instances.get(instance_name)
                getattr(instance, action)(wait=True, timeout=30)
                print(f"{action} completed on {label}.")
            except NotFound:
                self.lxd_error_message = f"Instance {label} not found."
            except Exception as e:
                self.lxd_error_message = f"Failed to {action} {label}: {e}"
            else:
                # Refresh right away so the new state shows up. On failure, leave
                # the error visible until the next periodic refresh instead.
                self._refresh_event.set()
                return
            print(f"Error: {self.lxd_error_message}")
            GLib.idle_add(self._build_or_update_menu)

    # ---- UI (GTK main thread) -------------------------------------------

    def _watch_theme(self):
        """Follows light/dark changes via the desktop portal, falling back to GTK."""
        try:
            self._portal = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                PORTAL_SETTINGS_IFACE,
                None,
            )
            self._portal.connect("g-signal", self._on_portal_signal)
            self._portal_color_scheme = self._read_portal_color_scheme()
        except GLib.Error as e:
            self._portal = None
            print(f"Desktop portal unavailable, using GTK theme for icon: {e.message}")

        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            for prop in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
                gtk_settings.connect(f"notify::{prop}", lambda *_a: self._update_icon())

    def _read_portal_color_scheme(self):
        args = GLib.Variant("(ss)", (APPEARANCE_NAMESPACE, COLOR_SCHEME_KEY))
        # ReadOne is the current API; older portals only have the deprecated Read.
        for method in ("ReadOne", "Read"):
            try:
                result = self._portal.call_sync(
                    method, args, Gio.DBusCallFlags.NONE, 2000, None
                )
                return int(result.unpack()[0])
            except (GLib.Error, TypeError, ValueError):
                continue
        return 0

    def _on_portal_signal(self, _proxy, _sender, signal_name, parameters):
        if signal_name != "SettingChanged":
            return
        namespace, key, value = parameters.unpack()
        if namespace == APPEARANCE_NAMESPACE and key == COLOR_SCHEME_KEY:
            self._portal_color_scheme = int(value)
            self._update_icon()

    def _is_dark_theme(self):
        if self._portal_color_scheme in (1, 2):
            return self._portal_color_scheme == 1
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is None:
            return False
        if gtk_settings.get_property("gtk-application-prefer-dark-theme"):
            return True
        theme = (gtk_settings.get_property("gtk-theme-name") or "").lower()
        return "dark" in theme

    def _update_icon(self):
        # Alert when LXD can't be reached: no client and a reason to show.
        if self.lxd_client is None and self.lxd_error_message:
            icon, description = ICON_ALERT, "LXD unavailable"
        elif self._is_dark_theme():
            icon, description = ICON_DARK_THEME, "LXD"
        else:
            icon, description = ICON_LIGHT_THEME, "LXD"
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
    for icon_path in (ICON_LIGHT_THEME, ICON_DARK_THEME, ICON_ALERT):
        if not os.path.exists(icon_path):
            print(f"Warning: icon not found at {icon_path}")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = LXDIndicatorApp()
    Gtk.main()
