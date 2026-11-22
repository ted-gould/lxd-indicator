"""Tests for lxd-indicator.py's error paths, loops and D-Bus plumbing."""

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from pylxd.exceptions import ClientConnectionFailed, LXDAPIException

from test_indicator import CONFIG, LOCAL, FakeInstance, FakeServers, ind
from test_indicator import IndicatorTestCase

# The real loops; IndicatorTestCase patches them out of the app.
EVENT_LOOP = ind.LXDIndicatorApp._event_loop
REFRESH_LOOP = ind.LXDIndicatorApp._refresh_loop


class Stop(Exception):
    """Breaks out of the app's endless loops."""


class FakeGLibError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def api_error(message, status=500):
    response = mock.Mock(status_code=status)
    response.json.return_value = {"error": message}
    return LXDAPIException(response)


class LocalErrorsTest(IndicatorTestCase):
    def setUp(self):
        super().setUp()
        self.servers = FakeServers({LOCAL: [FakeInstance("web")]})
        self.app = self.make_app(self.servers)
        self.app._refresh_once()

    def test_api_error_keeps_the_client(self):
        client = self.app.lxd_client
        client.instances.all = mock.Mock(side_effect=api_error("boom"))
        self.assertEqual(self.app._fetch_instances(), [])
        self.assertEqual(self.app.lxd_error_message, "LXD API error: boom")
        self.assertIs(self.app.lxd_client, client)

    def test_lost_connection_drops_the_client_and_reconnects(self):
        self.app.lxd_client.instances.all = mock.Mock(
            side_effect=ConnectionError("reset")
        )
        self.app._refresh_once()
        self.assertIsNone(self.app.lxd_client)
        self.assertEqual(self.app.lxd_error_message, "Lost connection to LXD: reset")
        self.app._refresh_once()
        self.assertEqual(self.servers.endpoints().count(LOCAL), 2)
        self.assertIsNone(self.app.lxd_error_message)
        self.assertIn("web", self.labels(self.app))

    def test_api_error_on_connect_shows_the_alert_icon(self):
        self.app.lxd_client = None
        with mock.patch.object(ind, "Client", side_effect=api_error("nope")):
            self.app._connect_lxd()
        self.assertIsNone(self.app.lxd_client)
        self.assertEqual(
            self.app.lxd_error_message, "LXD API error during connection: nope"
        )
        self.app._current_icon = None
        self.app._update_icon()
        self.app.indicator.set_icon_full.assert_called_with(
            ind.ICON_ALERT, "LXD unavailable"
        )

    def test_failed_connection_is_diagnosed(self):
        app = self.make_app(FakeServers({}, unreachable={LOCAL}))
        app._refresh_once()
        self.assertEqual(
            app.lxd_error_message,
            "LXD socket not found at local.socket. Is LXD installed and running?",
        )


class DiagnosisTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket = os.path.join(tmp.name, "unix.socket")
        open(self.socket, "w").close()
        patch = mock.patch.object(ind, "_snap_plug_connected", return_value=None)
        self.plug = patch.start()
        self.addCleanup(patch.stop)

    def diagnose(self, error="x"):
        return ind._diagnose_connection_failure(self.socket, OSError(error))

    def test_unconnected_lxd_plug(self):
        self.plug.return_value = False
        self.assertIn("sudo snap connect lxd-indicator:lxd lxd", self.diagnose())

    def test_missing_socket(self):
        os.remove(self.socket)
        self.assertIn(f"LXD socket not found at {self.socket}", self.diagnose())

    def test_permission_denied(self):
        self.assertIn("usermod -aG lxd", self.diagnose("[Errno 13] Permission denied"))
        with mock.patch.object(ind.os, "access", return_value=False):
            self.assertIn("usermod -aG lxd", self.diagnose())

    def test_other_errors_are_passed_on(self):
        self.assertEqual(self.diagnose(), "LXD connection failed: x")


class RemoteErrorsTest(IndicatorTestCase):
    def setUp(self):
        super().setUp()
        self.servers = FakeServers(
            {LOCAL: [], "https://zeta:8443": [FakeInstance("a")]}
        )
        self.app = self.make_app(self.servers)
        self.app._refresh_once()

    def test_unexpected_error_drops_the_client(self):
        client = self.app.remote_clients["zeta"][1]
        client.instances.all = mock.Mock(side_effect=ConnectionError("reset"))
        self.app._refresh_once()
        self.assertEqual(self.app.remote_errors["zeta"], "Connection failed: reset")
        self.assertNotIn("zeta", self.app.remote_clients)

    def test_connection_failure_without_a_message(self):
        with mock.patch.object(ind, "Client", side_effect=ClientConnectionFailed()):
            self.app.remote_clients.clear()
            self.app._refresh_once()
        self.assertEqual(self.app.remote_errors["zeta"], "Connection failed")

    def test_removing_every_remote_forgets_them_silently(self):
        self.app.notifier = mock.Mock()
        self.app._quiet_until = 0
        self.write_config("remotes: {local: {addr: 'unix://'}}")
        with mock.patch.object(ind.GLib, "idle_add", lambda f, *a: f(*a)):
            self.app._refresh_once()
        self.assertEqual(self.app.remote_clients, {})
        self.assertEqual(self.app.remote_errors, {})
        self.assertEqual(list(self.app._known_instances), [None])
        self.app.notifier.instance_changes.assert_not_called()

    def test_unreadable_config_keeps_the_baseline(self):
        self.app.notifier = mock.Mock()
        self.app._quiet_until = 0
        with mock.patch.object(ind.GLib, "idle_add", lambda f, *a: f(*a)):
            with mock.patch.object(ind, "_load_remotes", side_effect=OSError("io")):
                self.app._refresh_once()
            self.servers.instances["https://zeta:8443"].append(FakeInstance("new"))
            self.write_config(CONFIG)
            self.app._refresh_once()
        self.app.notifier.instance_changes.assert_called_once_with(
            "zeta", "new", ["created", "started"]
        )

    def test_menu_is_rebuilt_only_when_something_changed(self):
        with mock.patch.object(ind.GLib, "idle_add") as idle_add:
            self.app._refresh_once()
            idle_add.assert_not_called()
            self.servers.instances["https://zeta:8443"][0].status = "Stopped"
            self.app._refresh_once()
            idle_add.assert_called_once_with(self.app._build_or_update_menu)


class LoopsTest(IndicatorTestCase):
    def setUp(self):
        super().setUp()
        self.app = self.make_app(FakeServers({LOCAL: []}))
        self.app._refresh_once()

    def test_watching_events_hooks_up_the_callback_then_refreshes(self):
        ws = mock.Mock()
        self.app.lxd_client = mock.Mock()
        self.app.lxd_client.events.return_value = ws
        ws.run.side_effect = lambda: self.assertTrue(self.app._refresh_event.is_set())
        self.app._watch_events_once()
        self.app.lxd_client.events.assert_called_once_with(
            websocket_client=ind._LifecycleEventClient,
            event_types={ind.EventType.Lifecycle},
        )
        self.assertEqual(ws.on_event, self.app._on_lxd_event)
        ws.connect.assert_called_once_with()
        ws.run.assert_called_once_with()

    def test_watching_events_without_a_client_does_nothing(self):
        self.app.lxd_client = None
        self.app._watch_events_once()
        self.assertFalse(self.app._refresh_event.is_set())

    def test_event_loop_retries_after_errors(self):
        with (
            mock.patch.object(
                self.app, "_watch_events_once", side_effect=OSError("down")
            ),
            mock.patch.object(ind.time, "sleep", side_effect=[None, Stop]) as sleep,
        ):
            with self.assertRaises(Stop):
                EVENT_LOOP(self.app)
        sleep.assert_called_with(ind.EVENT_RETRY_INTERVAL)
        self.assertEqual(sleep.call_count, 2)

    def test_refresh_loop_clears_the_request_before_refreshing(self):
        seen = []

        def refresh():
            seen.append(self.app._refresh_event.is_set())

        self.app._refresh_event.set()
        with (
            mock.patch.object(self.app, "_refresh_once", refresh),
            mock.patch.object(self.app._refresh_event, "wait", side_effect=Stop),
        ):
            with self.assertRaises(Stop):
                REFRESH_LOOP(self.app)
        self.assertEqual(seen, [False])

    def test_lifecycle_events_without_an_action_are_ignored(self):
        for event in ({}, {"metadata": None}, {"metadata": {"action": None}}):
            self.app._on_lxd_event(event)
        self.assertFalse(self.app._refresh_event.is_set())

    def test_menu_actions_run_on_a_thread(self):
        with mock.patch.object(ind.threading, "Thread") as thread:
            handler = self.app._in_background(self.app._run_action, None, "web", "stop")
            handler(mock.sentinel.widget)
        thread.assert_called_once_with(
            target=self.app._run_action, args=(None, "web", "stop"), daemon=True
        )
        thread.return_value.start.assert_called_once_with()


class AutostartTest(IndicatorTestCase):
    def setUp(self):
        super().setUp()
        snap = os.path.join(self.tmp.name, "snap")
        os.makedirs(os.path.join(snap, "meta", "gui"))
        self.source = os.path.join(snap, "meta", "gui", "lxd-indicator.desktop")
        with open(self.source, "w") as f:
            f.write("[Desktop Entry]\n")
        self.dir = os.path.join(self.tmp.name, "autostart")
        self.file = os.path.join(self.dir, "lxd-indicator.desktop")
        for name, value in [
            ("SNAP_DIR", snap),
            ("AUTOSTART_DIR", self.dir),
            ("AUTOSTART_FILE", self.file),
        ]:
            patch = mock.patch.object(ind, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.app = self.make_app(FakeServers({LOCAL: []}))

    def test_enabling_copies_the_snaps_launcher(self):
        ind.enable_autostart()
        with open(self.file) as f:
            self.assertEqual(f.read(), "[Desktop Entry]\n")

    def test_enabling_keeps_an_existing_launcher(self):
        os.makedirs(self.dir)
        with open(self.file, "w") as f:
            f.write("edited\n")
        ind.enable_autostart()
        with open(self.file) as f:
            self.assertEqual(f.read(), "edited\n")

    def test_enabling_failure_is_logged(self):
        with mock.patch.object(ind.shutil, "copyfile", side_effect=PermissionError):
            ind.enable_autostart()
        print.assert_any_call("Failed to enable autostart: ")

    def test_disabling_removes_it(self):
        ind.enable_autostart()
        ind.disable_autostart()
        self.assertFalse(os.path.exists(self.file))
        ind.disable_autostart()  # already gone: nothing to do

    def test_disabling_failure_is_logged(self):
        with mock.patch.object(ind.os, "remove", side_effect=PermissionError):
            ind.disable_autostart()
        print.assert_any_call("Failed to disable autostart: ")

    def test_nothing_happens_outside_the_snap(self):
        with mock.patch.object(ind, "SNAP_DIR", None):
            ind.enable_autostart()
            self.assertFalse(os.path.exists(self.file))
        ind.enable_autostart()
        with mock.patch.object(ind, "SNAP_DIR", None):
            ind.disable_autostart()
        self.assertTrue(os.path.exists(self.file))

    def test_remove_disables_autostart_and_quits(self):
        ind.enable_autostart()
        ind.Gtk.main_quit.reset_mock()
        self.app._on_remove(mock.sentinel.widget)
        self.assertFalse(os.path.exists(self.file))
        ind.Gtk.main_quit.assert_called_once_with()

    def test_menu_offers_remove_instead_of_quit(self):
        ind.Gtk.MenuItem.reset_mock()
        self.app._build_or_update_menu()
        labels = [c.kwargs.get("label") for c in ind.Gtk.MenuItem.call_args_list]
        self.assertIn("Remove", labels)
        self.assertNotIn("Quit", labels)

    def test_no_start_at_login_menu_item(self):
        ind.Gtk.CheckMenuItem.reset_mock()
        self.app._build_or_update_menu()
        ind.Gtk.CheckMenuItem.assert_not_called()


class NotifierDBusTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch("builtins.print"),
            mock.patch.object(ind.GLib, "Error", FakeGLibError),
            mock.patch.object(ind.GLib, "Variant", lambda *a: ("Variant",) + a),
            mock.patch.object(
                ind.GLib, "markup_escape_text", lambda t: t.replace("<", "&lt;")
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.proxy = mock.Mock()
        self.capabilities = ["body", "body-markup"]
        self.proxy.call_sync.side_effect = lambda *a: mock.Mock(
            unpack=lambda: (self.capabilities,)
        )
        patch = mock.patch.object(
            ind.Gio.DBusProxy, "new_for_bus_sync", return_value=self.proxy
        )
        patch.start()
        self.addCleanup(patch.stop)

    def notify(self, notifier, body):
        done = mock.Mock()
        notifier._dbus_notify(7, "Summary", body, done)
        method, (_, signature, args), *_rest, finish = self.proxy.call.call_args.args
        return method, signature, args, finish, done

    def test_notify_packs_the_arguments_and_reports_the_id(self):
        method, signature, args, finish, done = self.notify(ind.Notifier(), "b")
        self.assertEqual((method, signature), ("Notify", "(susssasa{sv}i)"))
        self.assertEqual(
            args,
            (
                ind.APP_NAME,
                7,
                ind.ICON_ALERT,
                "Summary",
                "b",
                [],
                {"desktop-entry": ("Variant", "s", ind.DESKTOP_ENTRY)},
                -1,
            ),
        )
        reply = mock.Mock()
        reply.call_finish.return_value.unpack.return_value = (42,)
        finish(reply, None)
        done.assert_called_once_with(42)

    def test_failed_notify_reports_zero(self):
        *_, finish, done = self.notify(ind.Notifier(), "b")
        reply = mock.Mock()
        reply.call_finish.side_effect = FakeGLibError("gone")
        finish(reply, None)
        done.assert_called_once_with(0)

    def test_no_notification_service(self):
        self.proxy.call_sync.side_effect = None
        with mock.patch.object(
            ind.Gio.DBusProxy, "new_for_bus_sync", side_effect=FakeGLibError("no bus")
        ):
            notifier = ind.Notifier()
        self.assertIsNone(notifier._proxy)
        done = mock.Mock()
        notifier._dbus_notify(0, "s", "b", done)
        done.assert_called_once_with(0)

    def sent_body(self, error):
        notifier = ind.Notifier()
        notifier.action_failed("zeta", "web", "Start", error)
        return self.proxy.call.call_args.args[1][2][4]

    def test_server_text_is_escaped_for_markup_servers(self):
        self.assertEqual(
            self.sent_body("<a href='x'>click</a>"),
            "zeta:web: &lt;a href='x'>click&lt;/a>",
        )

    def test_server_text_is_sent_as_is_without_markup_support(self):
        self.capabilities = ["body"]
        self.assertEqual(self.sent_body("<b>"), "zeta:web: <b>")

    def test_long_server_errors_are_cut_short(self):
        body = self.sent_body("x" * 1000)
        self.assertEqual(len(body), len("zeta:web: ") + ind.MAX_ERROR_LENGTH)
        self.assertTrue(body.endswith("…"))

    def test_other_signals_are_ignored(self):
        notifier = ind.Notifier()
        notifier._entries[("k",)] = {"id": 7, "busy": False}
        notifier._on_signal(None, None, "ActionInvoked", mock.Mock())
        self.assertIn(("k",), notifier._entries)


class HelpersTest(unittest.TestCase):
    def test_socket_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = os.path.join(tmp, "a"), os.path.join(tmp, "b")
            with (
                mock.patch.object(ind, "LXD_SOCKET_CANDIDATES", [first, second]),
                mock.patch.dict(os.environ),
            ):
                os.environ.pop("LXD_DIR", None)
                self.assertEqual(ind._lxd_socket_path(), first)  # the default
                open(second, "w").close()
                self.assertEqual(ind._lxd_socket_path(), second)
                os.environ["LXD_DIR"] = "/custom"
                self.assertEqual(ind._lxd_socket_path(), "/custom/unix.socket")

    def test_snap_plug_connected(self):
        with mock.patch.object(ind, "SNAP_DIR", None):
            self.assertIsNone(ind._snap_plug_connected("lxd"))
        with (
            mock.patch.object(ind, "SNAP_DIR", "/snap/x"),
            mock.patch.object(ind.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertIs(ind._snap_plug_connected("lxd"), True)
            self.assertEqual(run.call_args.args[0], ["snapctl", "is-connected", "lxd"])
            run.return_value.returncode = 1
            self.assertIs(ind._snap_plug_connected("lxd"), False)
            run.side_effect = subprocess.TimeoutExpired("snapctl", 5)
            self.assertIsNone(ind._snap_plug_connected("lxd"))

    def test_no_lxc_config_means_no_remotes(self):
        with mock.patch.object(ind, "_lxc_config_dir", return_value=None):
            self.assertEqual(ind._load_remotes(), (None, {}))

    def test_lifecycle_client_ignores_undecodable_messages(self):
        ws = ind._LifecycleEventClient("ws+unix:///nonexistent.socket")
        self.addCleanup(ws.sock.close)
        ws.on_event = mock.Mock()
        ws.received_message(types.SimpleNamespace(data=b"\xff"))
        ws.on_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
