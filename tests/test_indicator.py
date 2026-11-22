"""Unit tests for lxd-indicator.py.

GTK, GLib and the app indicator are replaced with mocks and pylxd's Client
with a fake, so these run headless and without an LXD daemon.
"""

import importlib.util
import os
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock

from pylxd.exceptions import ClientConnectionFailed, LXDAPIException

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lxd-indicator.py"
)


def _load_module():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None
    repository = types.ModuleType("gi.repository")
    for name in ("AyatanaAppIndicator3", "Gio", "GLib", "Gtk"):
        setattr(repository, name, mock.MagicMock(name=name))
    with mock.patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        spec = importlib.util.spec_from_file_location("lxd_indicator", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


ind = _load_module()


class FakeInstance:
    def __init__(self, name, status="Running"):
        self.name = name
        self.status = status
        self.actions = []

    def start(self, wait, timeout):
        self.actions.append("start")

    def stop(self, wait, timeout):
        self.actions.append("stop")


class FakeServers:
    """Stands in for pylxd.Client, serving instances per endpoint."""

    def __init__(self, instances, unreachable=()):
        self.instances = instances  # endpoint -> [FakeInstance]
        self.unreachable = set(unreachable)
        self.created = []  # kwargs of every Client() call

    def __call__(
        self, endpoint=None, cert=None, verify=True, timeout=None, project=None
    ):
        self.created.append(
            dict(
                endpoint=endpoint,
                cert=cert,
                verify=verify,
                timeout=timeout,
                project=project,
            )
        )
        if endpoint in self.unreachable:
            raise ClientConnectionFailed("no route to host")
        found = self.instances.get(endpoint, [])
        by_name = {i.name: i for i in found}
        return types.SimpleNamespace(
            instances=types.SimpleNamespace(
                all=lambda: list(found), get=lambda name: by_name[name]
            )
        )

    def endpoints(self):
        return [c["endpoint"] for c in self.created]


LOCAL = "http+unix://local.socket"

CONFIG = """\
default-remote: local
remotes:
  local: {addr: "unix://", public: false}
  images: {addr: "https://images.lxd.canonical.com", protocol: simplestreams, public: true}
  pub: {addr: "https://pub.example:8443", protocol: lxd, public: true}
  zeta: {addr: "https://zeta:8443", protocol: lxd, auth_type: tls}
  alpha: {addr: "https://alpha:8443", auth_type: tls, project: dev}
  sso: {addr: "https://sso:8443", protocol: lxd, auth_type: oidc}
  sock: {addr: "unix:///other/unix.socket", protocol: lxd}
"""


class IndicatorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = self.tmp.name
        self.write_config(CONFIG)
        for name in ("client.crt", "client.key"):
            open(os.path.join(self.conf, name), "w").close()
        os.makedirs(os.path.join(self.conf, "servercerts"))
        open(os.path.join(self.conf, "servercerts", "zeta.crt"), "w").close()

        patches = [
            mock.patch.dict(os.environ, {"LXD_CONF": self.conf}),
            mock.patch.object(ind, "_lxd_socket_path", return_value="local.socket"),
            mock.patch.object(ind, "quote", lambda path, safe="": path),
            mock.patch.object(ind.LXDIndicatorApp, "_refresh_loop", lambda self: None),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write_config(self, text):
        with open(os.path.join(self.conf, "config.yml"), "w") as f:
            f.write(textwrap.dedent(text))

    def make_app(self, servers):
        patch = mock.patch.object(ind, "Client", servers)
        patch.start()
        self.addCleanup(patch.stop)
        return ind.LXDIndicatorApp()

    @staticmethod
    def labels(app):
        return [
            ind._instance_label(i["remote"], i["name"]) for i in app.lxd_instances_cache
        ]


class LoadRemotesTest(IndicatorTestCase):
    def test_only_lxd_https_remotes_with_tls_are_kept(self):
        config_dir, remotes = ind._load_remotes()
        self.assertEqual(config_dir, self.conf)
        self.assertEqual(sorted(remotes), ["alpha", "zeta"])

    def test_missing_config_means_no_remotes(self):
        os.remove(os.path.join(self.conf, "config.yml"))
        self.assertEqual(ind._load_remotes(), (self.conf, {}))

    def test_empty_config_means_no_remotes(self):
        self.write_config("")
        self.assertEqual(ind._load_remotes(), (self.conf, {}))

    def test_config_dir_candidates_are_searched_in_order(self):
        with tempfile.TemporaryDirectory() as home:
            snap_dir = os.path.join(home, "snap", "lxd", "common", "config")
            lxc_dir = os.path.join(home, ".config", "lxc")
            for path in (snap_dir, lxc_dir):
                os.makedirs(path)
            with (
                mock.patch.dict(os.environ),
                mock.patch.object(ind, "LXC_CONFIG_CANDIDATES", [snap_dir, lxc_dir]),
            ):
                del os.environ["LXD_CONF"]
                self.assertIsNone(ind._lxc_config_dir())
                open(os.path.join(lxc_dir, "config.yml"), "w").close()
                self.assertEqual(ind._lxc_config_dir(), lxc_dir)
                open(os.path.join(snap_dir, "config.yml"), "w").close()
                self.assertEqual(ind._lxc_config_dir(), snap_dir)


class ListingTest(IndicatorTestCase):
    SERVERS = {
        LOCAL: [FakeInstance("zz"), FakeInstance("aa", "Stopped")],
        "https://zeta:8443": [FakeInstance("b"), FakeInstance("a")],
        "https://alpha:8443": [FakeInstance("web"), FakeInstance("db", "Frozen")],
    }

    def test_local_first_then_remotes_alphabetically(self):
        app = self.make_app(FakeServers(self.SERVERS))
        app._refresh_once()
        self.assertEqual(
            self.labels(app), ["aa", "zz", "alpha:db", "alpha:web", "zeta:a", "zeta:b"]
        )
        self.assertEqual(app.remote_errors, {})
        self.assertEqual(
            [i["status"] for i in app.lxd_instances_cache][:3],
            ["stopped", "running", "frozen"],
        )

    def test_menu_labels_follow_the_same_order(self):
        app = self.make_app(FakeServers(self.SERVERS))
        app._refresh_once()
        ind.Gtk.Label.reset_mock()
        app._build_or_update_menu()
        shown = [c.kwargs["label"] for c in ind.Gtk.Label.call_args_list]
        self.assertEqual(
            shown, ["aa", "zz", "alpha:db", "alpha:web", "zeta:a", "zeta:b"]
        )

    def test_remote_clients_use_lxc_certificates_and_project(self):
        servers = FakeServers(self.SERVERS)
        app = self.make_app(servers)
        app._refresh_once()
        created = {c["endpoint"]: c for c in servers.created}
        cert = (
            os.path.join(self.conf, "client.crt"),
            os.path.join(self.conf, "client.key"),
        )
        zeta, alpha = created["https://zeta:8443"], created["https://alpha:8443"]
        self.assertEqual(zeta["cert"], cert)
        self.assertEqual(
            zeta["verify"], os.path.join(self.conf, "servercerts", "zeta.crt")
        )
        self.assertIsNone(zeta["project"])
        self.assertEqual(zeta["timeout"], ind.REMOTE_TIMEOUT)
        self.assertIs(alpha["verify"], True)  # no pinned cert saved for alpha
        self.assertEqual(alpha["project"], "dev")

    def test_no_client_certificate_connects_without_one(self):
        os.remove(os.path.join(self.conf, "client.key"))
        servers = FakeServers(self.SERVERS)
        self.make_app(servers)._refresh_once()
        remote = [c for c in servers.created if c["endpoint"] != LOCAL]
        self.assertTrue(remote)
        self.assertTrue(all(c["cert"] is None for c in remote))

    def test_unreachable_remote_reports_error_and_others_still_list(self):
        servers = FakeServers(self.SERVERS, unreachable={"https://alpha:8443"})
        app = self.make_app(servers)
        app._refresh_once()
        self.assertEqual(self.labels(app), ["aa", "zz", "zeta:a", "zeta:b"])
        self.assertEqual(list(app.remote_errors), ["alpha"])
        self.assertIn("no route to host", app.remote_errors["alpha"])
        self.assertIsNone(app.lxd_error_message)

        ind.Gtk.MenuItem.reset_mock()
        app._build_or_update_menu()
        items = [c.kwargs.get("label") for c in ind.Gtk.MenuItem.call_args_list]
        self.assertIn(f"alpha: {app.remote_errors['alpha']}", items)

        # The remote is retried, and recovers, on the next refresh.
        servers.unreachable.clear()
        app._refresh_once()
        self.assertEqual(app.remote_errors, {})
        self.assertIn("alpha:db", self.labels(app))

    def test_api_error_on_listing_drops_the_client(self):
        servers = FakeServers(self.SERVERS)
        app = self.make_app(servers)
        app._refresh_once()
        client = app.remote_clients["zeta"][1]
        response = mock.Mock(status_code=403)
        response.json.return_value = {"error": "not authorized"}
        client.instances.all = mock.Mock(side_effect=LXDAPIException(response))
        app._refresh_once()
        self.assertEqual(app.remote_errors["zeta"], "LXD API error: not authorized")
        self.assertNotIn("zeta", app.remote_clients)

    def test_clients_are_reused_between_refreshes(self):
        servers = FakeServers(self.SERVERS)
        app = self.make_app(servers)
        app._refresh_once()
        app._refresh_once()
        self.assertEqual(servers.endpoints().count("https://zeta:8443"), 1)

    def test_config_changes_are_picked_up(self):
        servers = FakeServers(self.SERVERS)
        app = self.make_app(servers)
        app._refresh_once()
        self.write_config("""\
            remotes:
              zeta: {addr: "https://zeta:8443", protocol: lxd, project: other}
            """)
        app._refresh_once()
        self.assertEqual(sorted(app.remote_clients), ["zeta"])
        self.assertEqual(servers.created[-1]["project"], "other")
        self.assertEqual(self.labels(app), ["aa", "zz", "zeta:a", "zeta:b"])

    def test_unreadable_config_reports_error_but_lists_local(self):
        app = self.make_app(FakeServers(self.SERVERS))
        with mock.patch.object(
            ind, "_load_remotes", side_effect=PermissionError("denied")
        ):
            app._refresh_once()
        self.assertEqual(self.labels(app), ["aa", "zz"])
        self.assertIn("denied", app.remote_errors["remotes"])

    def test_unreadable_config_in_snap_suggests_connecting_plug(self):
        app = self.make_app(FakeServers(self.SERVERS))
        with (
            mock.patch.object(
                ind, "_load_remotes", side_effect=PermissionError("denied")
            ),
            mock.patch.object(ind, "_snap_plug_connected", return_value=False),
        ):
            app._refresh_once()
        self.assertIn(
            "snap connect lxd-indicator:lxc-config", app.remote_errors["remotes"]
        )

    def test_local_failure_still_lists_remotes(self):
        servers = FakeServers(self.SERVERS, unreachable={LOCAL})
        app = self.make_app(servers)
        app._refresh_once()
        self.assertIsNotNone(app.lxd_error_message)
        self.assertEqual(
            self.labels(app), ["alpha:db", "alpha:web", "zeta:a", "zeta:b"]
        )


class ActionTest(IndicatorTestCase):
    def setUp(self):
        super().setUp()
        self.local_web = FakeInstance("web")
        self.remote_web = FakeInstance("web")
        self.servers = FakeServers(
            {LOCAL: [self.local_web], "https://zeta:8443": [self.remote_web]}
        )
        self.app = self.make_app(self.servers)
        self.app._refresh_once()

    def test_action_on_remote_instance_goes_to_that_remote(self):
        self.app._run_action("zeta", "web", "stop")
        self.assertEqual(self.remote_web.actions, ["stop"])
        self.assertEqual(self.local_web.actions, [])
        self.assertTrue(self.app._refresh_event.is_set())

    def test_action_on_local_instance_goes_to_local(self):
        self.app._run_action(None, "web", "start")
        self.assertEqual(self.local_web.actions, ["start"])
        self.assertEqual(self.remote_web.actions, [])

    def test_action_on_unknown_remote_reports_error(self):
        self.app._run_action("gone", "web", "stop")
        self.assertIn("gone:web", self.app.lxd_error_message)
        self.assertFalse(self.app._refresh_event.is_set())


class HelpersTest(unittest.TestCase):
    def test_instance_label(self):
        self.assertEqual(ind._instance_label(None, "web"), "web")
        self.assertEqual(ind._instance_label("zeta", "web"), "zeta:web")

    def test_sort_key_orders_local_before_remotes(self):
        infos = [
            {"remote": r, "name": n}
            for r, n in [("b", "x"), (None, "z"), ("a", "y"), (None, "a"), ("a", "b")]
        ]
        ordered = [(i["remote"], i["name"]) for i in sorted(infos, key=ind._sort_key)]
        self.assertEqual(
            ordered, [(None, "a"), (None, "z"), ("a", "b"), ("a", "y"), ("b", "x")]
        )


if __name__ == "__main__":
    unittest.main()
