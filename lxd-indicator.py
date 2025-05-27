#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
from gi.repository import Gtk, AppIndicator3, GLib

import os
import signal
import threading
import time
import functools
from pylxd import client as pylxd_client
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

APPINDICATOR_ID = 'lxd-status-indicator'
# Path to the LXD logo icon. Ensure this path is correct.
SNAP_DIR = os.environ.get('SNAP')
if SNAP_DIR:
    LXD_MAIN_ICON_PATH = os.path.join(SNAP_DIR, 'lxd_logo.png')
else:
    # Fallback for running outside a snap (e.g., during development)
    # Assumes script is in the project root alongside lxd_logo.png for local dev.
    LXD_MAIN_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lxd_logo.png')
REFRESH_INTERVAL = 10  # seconds

# Freedesktop icon names for instance states
ICON_RUNNING = 'media-playback-start-symbolic'
ICON_STOPPED = 'media-playback-stop-symbolic'
ICON_PAUSED = 'media-playback-pause-symbolic' # For "Frozen" state
ICON_ERROR = 'dialog-error-symbolic'
ICON_PENDING = 'view-refresh-symbolic' # For transitional states or unknown

class LXDIndicatorApp:
    def __init__(self):
        self.lxd_client = None
        self.use_legacy_api = False # Flag to determine if using containers (True) or instances (False)
        self.indicator = AppIndicator3.Indicator.new(
            APPINDICATOR_ID,
            LXD_MAIN_ICON_PATH, # Use the resolved path
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)

        self.lxd_instances_cache = [] # Initialize as an empty list
        self.lxd_error_message = None # To store any persistent LXD connection error

        self._connect_lxd_initial() # Initial attempt to connect

        self.refresh_thread = threading.Thread(target=self._periodic_refresh_lxd_data)
        self.refresh_thread.daemon = True
        self.refresh_thread.start()

        # Initial menu build based on initial connection attempt
        GLib.idle_add(self._build_or_update_menu)


    def _connect_lxd_initial(self):
        """Attempts to connect to LXD and determines API version."""
        try:
            self.lxd_client = pylxd_client.Client()
            # Try to determine the API version
            try:
                self.lxd_client.instances.all() # Test for modern 'instances' API
                self.use_legacy_api = False
                self.lxd_error_message = None
                print("Successfully connected to LXD using modern 'instances' API.")
            except AttributeError:
                print("'instances' API not found, trying legacy 'containers' API.")
                self.lxd_client.containers.all() # Test for legacy 'containers' API
                self.use_legacy_api = True
                self.lxd_error_message = None
                print("Successfully connected to LXD using legacy 'containers' API.")
        except ClientConnectionFailed:
            self.lxd_client = None
            self.lxd_error_message = "LXD Connection Failed: Daemon unreachable or permission issue."
            print(f"Error: {self.lxd_error_message}")
        except LXDAPIException as e:
            self.lxd_client = None
            self.lxd_error_message = f"LXD API Error during connection: {e}"
            print(f"Error: {self.lxd_error_message}")
        except AttributeError: # Catch if neither.instances nor.containers exist after client init
            self.lxd_client = None
            self.lxd_error_message = "Failed to detect usable pylxd API (neither instances nor containers found)."
            print(f"Error: {self.lxd_error_message}")
        except Exception as e: # Catch any other unexpected error during init
            self.lxd_client = None
            self.lxd_error_message = f"Unexpected error connecting to LXD: {e}"
            print(f"Error: {self.lxd_error_message}")


    def _fetch_lxd_instances_with_error_handling(self):
        """Fetches LXD instances/containers and handles errors, updating self.lxd_error_message."""
        if not self.lxd_client:
            self._connect_lxd_initial()
            if not self.lxd_client:
                return # Always return a list

        try:
            instances_data = []
            if self.use_legacy_api:
                lxd_items = self.lxd_client.containers.all()
            else:
                lxd_items = self.lxd_client.instances.all()

            for item in lxd_items:
                instances_data.append({
                    'name': item.name,
                    'status': item.status.lower(),
                    'type': getattr(item, 'type', 'container').lower()
                })
            self.lxd_error_message = None
            return instances_data
        except AttributeError as e:
            self.lxd_client = None
            self.lxd_error_message = f"LXD API Attribute Error during fetch: {e}. Re-initializing connection."
            print(f"Error during fetch: {self.lxd_error_message}")
            return
        except ClientConnectionFailed:
            self.lxd_client = None
            self.lxd_error_message = "LXD Connection Lost: Daemon unreachable."
            print(f"Error during fetch: {self.lxd_error_message}")
            return
        except LXDAPIException as e:
            self.lxd_error_message = f"LXD API Error during fetch: {e}"
            print(f"Error during fetch: {self.lxd_error_message}")
            return
        except Exception as e:
            self.lxd_error_message = f"Unexpected error fetching instances: {e}"
            print(f"Error during fetch: {self.lxd_error_message}")
            return


    def _periodic_refresh_lxd_data(self):
        """Target function for the refresh thread."""
        while True:
            fetched_data = self._fetch_lxd_instances_with_error_handling()
            
            if fetched_data is None: # Should ideally not happen if _fetch always returns a list
                print("Warning: _fetch_lxd_instances_with_error_handling returned None. Defaulting to.")
                fetched_data = []

            if fetched_data!= self.lxd_instances_cache or \
               (self.lxd_error_message and (not self.lxd_instances_cache or self.lxd_instances_cache is None)):
                self.lxd_instances_cache = fetched_data
                GLib.idle_add(self._build_or_update_menu)
            time.sleep(REFRESH_INTERVAL)

    def _build_or_update_menu(self):
        """Builds or rebuilds the indicator menu. Must be called from main GTK thread via GLib.idle_add."""
        for item in self.menu.get_children():
            self.menu.remove(item)

        if self.lxd_error_message:
            error_item = Gtk.MenuItem(label=self.lxd_error_message)
            error_item.set_sensitive(False)
            self.menu.append(error_item)
            self.menu.append(Gtk.SeparatorMenuItem())

        current_instances_to_iterate = self.lxd_instances_cache
        if current_instances_to_iterate is None: # Defensive check
            print("Warning: lxd_instances_cache is None in _build_or_update_menu. Displaying empty list.")
            current_instances_to_iterate = []

        for instance_info in current_instances_to_iterate:
            name = instance_info['name']
            status = instance_info['status']

            menu_item_instance = Gtk.MenuItem()
            item_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            status_icon_image = Gtk.Image()
            icon_name = ICON_PENDING
            if status == 'running':
                icon_name = ICON_RUNNING
            elif status == 'stopped':
                icon_name = ICON_STOPPED
            elif status == 'frozen':
                icon_name = ICON_PAUSED
            status_icon_image.set_from_icon_name(icon_name, Gtk.IconSize.MENU)
            item_box.pack_start(status_icon_image, False, False, 0)

            instance_label = Gtk.Label(label=name)
            item_box.pack_start(instance_label, True, True, 0)
            menu_item_instance.add(item_box)

            submenu_actions = Gtk.Menu()
            start_action_item = Gtk.MenuItem(label="Start")
            start_action_item.connect('activate', functools.partial(self._on_instance_action, name, 'start'))
            start_action_item.set_sensitive(status not in ['running', 'frozen'])

            stop_action_item = Gtk.MenuItem(label="Stop")
            stop_action_item.connect('activate', functools.partial(self._on_instance_action, name, 'stop'))
            stop_action_item.set_sensitive(status in ['running', 'frozen'])

            submenu_actions.append(start_action_item)
            submenu_actions.append(stop_action_item)
            menu_item_instance.set_submenu(submenu_actions)
            self.menu.append(menu_item_instance)

        if current_instances_to_iterate:
            self.menu.append(Gtk.SeparatorMenuItem())

        refresh_item = Gtk.MenuItem(label="Refresh Now")
        refresh_item.connect('activate', self._on_manual_refresh)
        self.menu.append(refresh_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect('activate', self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()

    def _on_instance_action(self, instance_name, action, _widget):
        """Handles start/stop actions for an instance."""
        print(f"Action: {action} on instance {instance_name}")
        if not self.lxd_client:
            print("LXD client not available. Action aborted.")
            self.lxd_error_message = "LXD client not available for action."
            GLib.idle_add(self._build_or_update_menu)
            return

        try:
            if self.use_legacy_api:
                instance = self.lxd_client.containers.get(instance_name)
            else:
                instance = self.lxd_client.instances.get(instance_name)
            
            current_status = instance.status.lower()
            if action == 'start':
                if current_status not in ['running', 'frozen']:
                    instance.start(wait=False, timeout=30)
                    print(f"Start command sent to {instance_name}.")
            elif action == 'stop':
                if current_status in ['running', 'frozen']:
                    instance.stop(wait=False, timeout=30)
                    print(f"Stop command sent to {instance_name}.")
        except NotFound:
            print(f"Error: Instance {instance_name} not found.")
            self.lxd_error_message = f"Instance {instance_name} not found."
        except LXDAPIException as e:
            print(f"LXD API Error performing action {action} on {instance_name}: {e}")
            self.lxd_error_message = f"API error on {instance_name}: {e}"
        except Exception as e:
            print(f"Unexpected error during action {action} on {instance_name}: {e}")
            self.lxd_error_message = f"Unexpected error on {instance_name}: {e}"
        
        self._on_manual_refresh(None)

    def _on_manual_refresh(self, _widget):
        """Handles the 'Refresh' menu item click."""
        print("Manual refresh triggered.")
        current_data = self._fetch_lxd_instances_with_error_handling()
        if current_data is None: # Defensive check
            current_data = []
        
        if current_data!= self.lxd_instances_cache or self.lxd_error_message:
            self.lxd_instances_cache = current_data
            GLib.idle_add(self._build_or_update_menu)

    def _on_quit(self, _widget):
        """Handles the 'Quit' menu item click."""
        print("Quitting LXD Indicator App.")
        Gtk.main_quit()

if __name__ == "__main__":
    if not os.path.exists(LXD_MAIN_ICON_PATH):
        print(f"Error: LXD main icon not found at {LXD_MAIN_ICON_PATH}")
        print("Please ensure lxd_logo.png is in the same directory as the script, or update LXD_MAIN_ICON_PATH.")

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = LXDIndicatorApp()
    Gtk.main()

