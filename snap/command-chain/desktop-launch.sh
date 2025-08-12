#!/bin/bash

set -e

echo "--- Debugging launch script ---"
echo "Running as user: $(whoami)"
echo "PATH is: $PATH"
echo "--- Contents of $SNAP/usr/bin ---"
ls -l "$SNAP/usr/bin" || echo "Could not list $SNAP/usr/bin"
echo "--- Contents of $SNAP/bin ---"
ls -l "$SNAP/bin" || echo "Could not list $SNAP/bin"
echo "---------------------------"

# Set XDG_DATA_DIRS to include snapped resources
export XDG_DATA_DIRS="$SNAP/usr/share:$SNAP/share:/usr/share:/var/lib/snapd/desktop"

# Set a fallback GTK theme to prevent crashes in environments without a configured theme
export GTK_THEME="Adwaita:dark"

# Ensure the icon theme cache is up-to-date
if [ -d "$SNAP/usr/share/icons" ]; then
  if [ ! -f "$SNAP/usr/share/icons/hicolor/index.theme" ]; then
    echo "Running gtk-update-icon-cache..."
    gtk-update-icon-cache -f -t "$SNAP/usr/share/icons/hicolor"
  fi
fi

# Update GDK pixbuf loader cache
if [ -n "$(find "$SNAP/usr/lib" -name 'gdk-pixbuf-2.0')" ]; then
  echo "Updating gdk-pixbuf loaders cache..."
  gdk-pixbuf-query-loaders --update-cache
fi

# Update GTK input module cache
if [ -n "$(find "$SNAP/usr/lib" -name 'gtk-3.0')" ]; then
  echo "Updating GTK IM modules cache..."
  gtk-query-immodules-3.0 --update-cache
fi

# Compile GSettings schemas
if [ -d "$SNAP/usr/share/glib-2.0/schemas" ]; then
  echo "Compiling GSettings schemas..."
  glib-compile-schemas "$SNAP/usr/share/glib-2.0/schemas"
fi

# Execute the main application
exec "$SNAP/bin/lxd-indicator.py"
