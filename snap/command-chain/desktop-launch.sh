#!/bin/bash

set -e

# Set XDG_DATA_DIRS to include snapped resources
export XDG_DATA_DIRS="$SNAP/usr/share:$SNAP/share:/usr/share:/var/lib/snapd/desktop"

# Set a fallback GTK theme to prevent crashes
export GTK_THEME="Adwaita:dark"

# --- GDK Pixbuf cache ---
# Create a writable cache file and export the environment variable
GDK_PIXBUF_CACHE_FILE="$SNAP_USER_DATA/.cache/gdk-pixbuf-loaders.cache"
mkdir -p "$(dirname "$GDK_PIXBUF_CACHE_FILE")"
# Find the query tool, as it's not always in the PATH
GDK_PIXBUF_QUERY_LOADERS=$(find "$SNAP" -name gdk-pixbuf-query-loaders | head -n 1)
if [ -n "$GDK_PIXBUF_QUERY_LOADERS" ]; then
  echo "Updating GDK pixbuf loaders cache..."
  "$GDK_PIXBUF_QUERY_LOADERS" > "$GDK_PIXBUF_CACHE_FILE"
  export GDK_PIXBUF_MODULE_FILE="$GDK_PIXBUF_CACHE_FILE"
else
  echo "WARNING: gdk-pixbuf-query-loaders not found."
fi

# --- GTK IM modules cache ---
# Create a writable cache file and export the environment variable
GTK_IM_MODULE_CACHE_FILE="$SNAP_USER_DATA/.cache/gtk-immodules.cache"
mkdir -p "$(dirname "$GTK_IM_MODULE_CACHE_FILE")"
# Find the query tool
GTK_QUERY_IMMODULES=$(find "$SNAP" -name gtk-query-immodules-3.0 | head -n 1)
if [ -n "$GTK_QUERY_IMMODULES" ]; then
  echo "Updating GTK IM modules cache..."
  "$GTK_QUERY_IMMODULES" > "$GTK_IM_MODULE_CACHE_FILE"
  export GTK_IM_MODULE_FILE="$GTK_IM_MODULE_CACHE_FILE"
else
  echo "WARNING: gtk-query-immodules-3.0 not found."
fi

# --- GSettings schemas ---
# Copy schemas to a writable location and compile them there
GSETTINGS_SCHEMA_DIR="$SNAP_USER_DATA/.local/share/glib-2.0/schemas"
mkdir -p "$GSETTINGS_SCHEMA_DIR"
if [ -d "$SNAP/usr/share/glib-2.0/schemas" ]; then
  echo "Copying and compiling GSettings schemas..."
  cp -r "$SNAP/usr/share/glib-2.0/schemas"/* "$GSETTINGS_SCHEMA_DIR"
  # Find the compile tool
  GLIB_COMPILE_SCHEMAS=$(find "$SNAP" -name glib-compile-schemas | head -n 1)
  if [ -n "$GLIB_COMPILE_SCHEMAS" ]; then
    "$GLIB_COMPILE_SCHEMAS" "$GSETTINGS_SCHEMA_DIR"
    export GSETTINGS_SCHEMA_DIR="$GSETTINGS_SCHEMA_DIR"
  else
    echo "WARNING: glib-compile-schemas not found."
  fi
else
    echo "WARNING: No GSettings schemas found to compile."
fi

# Execute the main application
exec "$SNAP/bin/lxd-indicator.py"
