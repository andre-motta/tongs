# Shared desktop package inputs

`tongs.desktop` and `tongs.png` are the stable desktop-menu inputs shared by
the archive and RPM producers. The RPM may install the entry with its system
`tongs` command. The per-user installer does not copy this static entry into the
user menu: it renders the same product metadata while binding `Exec` to the
absolute persistent Python environment that requested installation.

The archive retains both files below `runtime/share` so later package producers
can compare the prepared inputs and installed payload. The Electron application
also carries the same icon in `app.asar` for its native window metadata.
