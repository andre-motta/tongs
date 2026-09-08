# Shared desktop package inputs

`tongs.desktop` and `tongs.png` are the stable desktop-menu inputs shared by
the archive and RPM producers. The RPM may install the entry with its system
`tongs` command. The per-user installer does not copy this static entry into the
user menu: it renders the same product metadata while binding `Exec` to the
absolute persistent Python environment that requested installation.

The archive retains the entry under `runtime/share/applications` and the original
2048 by 2048 icon under conventional `runtime/share/pixmaps`. The entry's
`Icon=tongs` name resolves that `tongs.png` without falsely describing a smaller
hicolor asset. Later package producers can compare these prepared inputs and the
installed payload. The Electron application also carries the same icon in
`app.asar` for its native window metadata.
