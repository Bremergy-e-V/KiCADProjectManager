#!/usr/bin/env bash
# Build the AppImage: a PyInstaller build of the app wrapped into a single
# executable file that runs on most Linux distributions.
#
#   packaging/linux/build-appimage.sh <output.AppImage>
#
# Needs uv and appimagetool (set APPIMAGETOOL to its path if it is not on PATH).
# The binaries need the glibc of the build machine or newer, so the release
# workflow builds on the oldest distribution we support.
set -Eeuo pipefail

out="$(realpath -m "$1")"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
appimagetool="${APPIMAGETOOL:-appimagetool}"
build="$repo/build/appimage"
appdir="$build/AppDir"
name=kicad-project-manager

rm -rf "$build"
mkdir -p "$build" "$(dirname "$out")"

cd "$repo"
uv run --locked pyinstaller --noconfirm --clean --windowed \
  --name "$name" \
  --collect-data kicad_project_manager \
  --copy-metadata keyring \
  --collect-submodules keyring.backends \
  --distpath "$build/dist" --workpath "$build/work" --specpath "$build" \
  packaging/launcher.py

# Leave out libraries every desktop has. Copies from the (older) build system
# would be used instead of the system's and break things that load the
# system's own libraries: graphics drivers need the system's libstdc++,
# fontconfig must match the system's configuration, and Qt's GTK theme plugin
# (native dialogs and look on GTK desktops such as Cinnamon) must find the
# system's GTK together with the matching GLib. Like the AppImage excludelist.
internal="$build/dist/$name/_internal"
for lib in \
  libstdc++.so.6 libgcc_s.so.1 libz.so.1 libexpat.so.1 libfontconfig.so.1 libfreetype.so.6 libharfbuzz.so.0 \
  libgpg-error.so.0 libcom_err.so.2 libselinux.so.1 libmount.so.1 libblkid.so.1 libpcre2-8.so.0 \
  libX11.so.6 libX11-xcb.so.1 libxcb.so.1 libXau.so.6 libXdmcp.so.6 libXext.so.6 libXrender.so.1 libXfixes.so.3 \
  libXi.so.6 libXcursor.so.1 libXrandr.so.2 libXinerama.so.1 libXcomposite.so.1 libXdamage.so.1 \
  libglib-2.0.so.0 libgobject-2.0.so.0 libgio-2.0.so.0 libgmodule-2.0.so.0 libgthread-2.0.so.0 \
  libgtk-3.so.0 libgdk-3.so.0 libgdk_pixbuf-2.0.so.0 libatk-1.0.so.0 libatk-bridge-2.0.so.0 libatspi.so.0 \
  libcairo.so.2 libcairo-gobject.so.2 libpango-1.0.so.0 libpangocairo-1.0.so.0 libpangoft2-1.0.so.0 \
  libepoxy.so.0 libpixman-1.so.0 libthai.so.0 libdatrie.so.1 libfribidi.so.0 libgraphite2.so.3 libpng16.so.16 libjpeg.so.8
do
  rm -f "$internal/$lib"
done

mkdir -p "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps"
cp -a "$build/dist/$name" "$appdir/usr/lib/$name"
cp packaging/linux/$name.desktop "$appdir/$name.desktop"
cp packaging/linux/$name.desktop "$appdir/usr/share/applications/"
cp src/kicad_project_manager/resources/$name.svg "$appdir/$name.svg"
cp src/kicad_project_manager/resources/$name.svg "$appdir/usr/share/icons/hicolor/scalable/apps/"
ln -s "$name.svg" "$appdir/.DirIcon"
cat > "$appdir/AppRun" <<APPRUN
#!/bin/sh
here="\$(dirname "\$(readlink -f "\$0")")"
exec "\$here/usr/lib/$name/$name" "\$@"
APPRUN
chmod 755 "$appdir/AppRun"

# --appimage-extract-and-run: appimagetool is an AppImage itself, and build
# containers have no FUSE to mount it.
ARCH=x86_64 "$appimagetool" --appimage-extract-and-run --no-appstream "$appdir" "$out"
ls -l "$out"
