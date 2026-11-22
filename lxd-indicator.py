#!/usr/bin/env python3
"""System tray indicator showing the state of local LXD instances."""

import functools
import os
import shutil
import signal
import subprocess
import threading
from urllib.parse import quote

import gi

gi.require_version('Gtk', '3.0')
try:
    # Ubuntu 24.04+ ships the Ayatana fork; the old libappindicator is gone.
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
from gi.repository import Gio, GLib, Gtk

from pylxd import Client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

APPINDICATOR_ID = 'lxd-status-indicator'
APP_NAME = 'lxd-indicator'
REFRESH_INTERVAL = 10  # seconds

SNAP_DIR = os.environ.get('SNAP')
# In the snap the icons sit at $SNAP; in a source checkout, next to this file.
ICON_DIR = SNAP_DIR or os.path.dirname(os.path.abspath(__file__))
# Panel icons: monochrome to match the desktop theme, orange for alerts.
ICON_LIGHT_THEME = os.path.join(ICON_DIR, 'lxd_logo_light.png')  # white with black
ICON_DARK_THEME = os.path.join(ICON_DIR, 'lxd_logo_dark.png')    # black with white
ICON_ALERT = os.path.join(ICON_DIR, 'lxd_logo.png')              # orange with white

# XDG desktop portal, which reports the user's light/dark preference.
PORTAL_BUS_NAME = 'org.freedesktop.portal.Desktop'
PORTAL_OBJECT_PATH = '/org/freedesktop/portal/desktop'
PORTAL_SETTINGS_IFACE = 'org.freedesktop.portal.Settings'
APPEARANCE_NAMESPACE = 'org.freedesktop.appearance'
COLOR_SCHEME_KEY = 'color-scheme'  # 0 = no preference, 1 = dark, 2 = light

# Candidate LXD unix sockets, in order of preference. Inside a strict snap the
# `lxd` interface only grants access to the LXD snap's socket.
LXD_SOCKET_CANDIDATES = [
    '/var/snap/lxd/common/lxd/unix.socket',
    '/var/lib/lxd/unix.socket',
]

AUTOSTART_DIR = os.path.join(
    os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'autostart')
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, f'{APP_NAME}.desktop')

# Freedesktop icon names for instance states
ICON_RUNNING = 'media-playback-start-symbolic'
ICON_STOPPED = 'media-playback-stop-symbolic'
ICON_PAUSED = 'media-playback-pause-symbolic'  # "Frozen" state
ICON_PENDING = 'view-refresh-symbolic'  # transitional / unknown states


def _lxd_socket_path():
    if 'LXD_DIR' in os.environ:
        return os.path.join(os.environ['LXD_DIR'], 'unix.socket')
    for path in LXD_SOCKET_CANDIDATES:
        if os.path.exists(path):
            return path
    return LXD_SOCKET_CANDIDATES[0]


def _snap_plug_connected(plug):
    """Returns True/False for a snap plug's connection state, None if unknown."""
    if not SNAP_DIR:
        return None
    try:
        result = subprocess.run(['snapctl', 'is-connected', plug],
                                capture_output=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def _diagnose_connection_failure(socket_path, error):
    """Turns a low-level connection failure into an actionable message."""
    if _snap_plug_connected('lxd') is False:
        return ("LXD interface not connected. Run: "
                "sudo snap connect lxd-indicator:lxd lxd")
    if not os.path.exists(socket_path):
        return f"LXD socket not found at {socket_path}. Is LXD installed and running?"
    if 'Permission denied' in str(error) or not os.access(socket_path, os.R_OK | os.W_OK):
        return ("Permission denied on the LXD socket. Add yourself to the 'lxd' group "
                "(sudo usermod -aG lxd $USER) and log out and back in.")
    return f"LXD connection failed: {error}"


class LXDIndicatorApp:
    def __init__(self):
        self.lxd_client = None
        self.lxd_error_message = None
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
        self.indicator.set_title('LXD Indicator')
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
            self.lxd_client = Client(endpoint=f"http+unix://{quote(socket_path, safe='')}")
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
            instances = [{
                'name': item.name,
                'status': item.status.lower(),
                'type': getattr(item, 'type', 'container').lower(),
            } for item in self.lxd_client.instances.all()]
            self.lxd_error_message = None
            return sorted(instances, key=lambda i: i['name'])
        except LXDAPIException as e:
            self.lxd_error_message = f"LXD API error: {e}"
        except Exception as e:  # requests ConnectionError etc. — reconnect next time
            self.lxd_client = None
            self.lxd_error_message = f"Lost connection to LXD: {e}"
        print(f"Error during fetch: {self.lxd_error_message}")
        return []

    def _refresh_once(self):
        with self._lock:
            previous = (self.lxd_instances_cache, self.lxd_error_message)
            self.lxd_instances_cache = self._fetch_instances()
            if (self.lxd_instances_cache, self.lxd_error_message) != previous:
                GLib.idle_add(self._build_or_update_menu)

    def _refresh_loop(self):
        while True:
            self._refresh_once()
            self._refresh_event.wait(REFRESH_INTERVAL)
            self._refresh_event.clear()

    def _run_action(self, instance_name, action):
        with self._lock:
            try:
                if not self.lxd_client:
                    raise RuntimeError("LXD client not available")
                instance = self.lxd_client.instances.get(instance_name)
                getattr(instance, action)(wait=True, timeout=30)
                print(f"{action} completed on {instance_name}.")
            except NotFound:
                self.lxd_error_message = f"Instance {instance_name} not found."
            except Exception as e:
                self.lxd_error_message = f"Failed to {action} {instance_name}: {e}"
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
                Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, PORTAL_SETTINGS_IFACE, None)
            self._portal.connect('g-signal', self._on_portal_signal)
            self._portal_color_scheme = self._read_portal_color_scheme()
        except GLib.Error as e:
            self._portal = None
            print(f"Desktop portal unavailable, using GTK theme for icon: {e.message}")

        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            for prop in ('gtk-theme-name', 'gtk-application-prefer-dark-theme'):
                gtk_settings.connect(f'notify::{prop}', lambda *_a: self._update_icon())

    def _read_portal_color_scheme(self):
        args = GLib.Variant('(ss)', (APPEARANCE_NAMESPACE, COLOR_SCHEME_KEY))
        # ReadOne is the current API; older portals only have the deprecated Read.
        for method in ('ReadOne', 'Read'):
            try:
                result = self._portal.call_sync(
                    method, args, Gio.DBusCallFlags.NONE, 2000, None)
                return int(result.unpack()[0])
            except (GLib.Error, TypeError, ValueError):
                continue
        return 0

    def _on_portal_signal(self, _proxy, _sender, signal_name, parameters):
        if signal_name != 'SettingChanged':
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
        if gtk_settings.get_property('gtk-application-prefer-dark-theme'):
            return True
        theme = (gtk_settings.get_property('gtk-theme-name') or '').lower()
        return 'dark' in theme

    def _update_icon(self):
        # Alert when LXD can't be reached: no client and a reason to show.
        if self.lxd_client is None and self.lxd_error_message:
            icon, description = ICON_ALERT, 'LXD unavailable'
        elif self._is_dark_theme():
            icon, description = ICON_DARK_THEME, 'LXD'
        else:
            icon, description = ICON_LIGHT_THEME, 'LXD'
        if icon != self._current_icon:
            self._current_icon = icon
            self.indicator.set_icon_full(icon, description)

    def _build_or_update_menu(self):
        self._update_icon()

        for item in self.menu.get_children():
            self.menu.remove(item)

        if self.lxd_error_message:
            error_item = Gtk.MenuItem(label=self.lxd_error_message)
            error_item.set_sensitive(False)
            self.menu.append(error_item)
            self.menu.append(Gtk.SeparatorMenuItem())

        instances = list(self.lxd_instances_cache)
        for info in instances:
            name, status = info['name'], info['status']

            item = Gtk.MenuItem()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon_name = {
                'running': ICON_RUNNING,
                'stopped': ICON_STOPPED,
                'frozen': ICON_PAUSED,
            }.get(status, ICON_PENDING)
            box.pack_start(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU),
                           False, False, 0)
            box.pack_start(Gtk.Label(label=name, xalign=0), True, True, 0)
            item.add(box)

            submenu = Gtk.Menu()
            start = Gtk.MenuItem(label="Start")
            start.connect('activate', functools.partial(self._on_instance_action, name, 'start'))
            start.set_sensitive(status not in ('running', 'frozen'))
            stop = Gtk.MenuItem(label="Stop")
            stop.connect('activate', functools.partial(self._on_instance_action, name, 'stop'))
            stop.set_sensitive(status in ('running', 'frozen'))
            submenu.append(start)
            submenu.append(stop)
            item.set_submenu(submenu)
            self.menu.append(item)

        if instances:
            self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh Now")
        refresh_item.connect('activate', lambda _w: self._refresh_event.set())
        self.menu.append(refresh_item)

        autostart_item = Gtk.CheckMenuItem(label="Start at Login")
        autostart_item.set_active(os.path.exists(AUTOSTART_FILE))
        autostart_item.connect('toggled', self._on_autostart_toggled)
        self.menu.append(autostart_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect('activate', lambda _w: Gtk.main_quit())
        self.menu.append(quit_item)

        self.menu.show_all()
        return False  # one-shot when used with GLib.idle_add

    def _on_instance_action(self, instance_name, action, _widget):
        print(f"Action: {action} on instance {instance_name}")
        threading.Thread(target=self._run_action, args=(instance_name, action),
                         daemon=True).start()

    def _on_autostart_toggled(self, widget):
        # For snaps, snapd's `autostart:` support launches the app at login when
        # this file exists in $SNAP_USER_DATA/.config/autostart.
        try:
            if widget.get_active():
                source = os.path.join(SNAP_DIR or '', 'meta', 'gui', f'{APP_NAME}.desktop')
                if not os.path.exists(source):
                    source = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'snap', 'gui', f'{APP_NAME}.desktop')
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
