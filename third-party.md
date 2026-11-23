# Third-party software

The `wn share` command uses two programs that are not part of wn. wn does
not include, bundle, or modify them. On the first run that needs them, wn
downloads the unmodified official release, checks its SHA-256 hash against a
pinned value, and then installs or runs it.

## WinFsp

WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos

wn installs WinFsp in Windows so that rclone can mount a shared Mac folder in
Windows. wn downloads the official `winfsp-2.1.25156.msi` installer from the
WinFsp releases page.

*   License: GPLv3, with the WinFsp FLOSS exception
*   Project: https://github.com/winfsp/winfsp

## rclone

Copyright (C) 2012 by Nick Craig-Wood http://www.craig-wood.com/nick/

wn runs rclone on the Mac to serve a shared folder over SFTP and in Windows to
mount it. wn downloads the official rclone v1.76.0 zip files from
https://downloads.rclone.org.

*   License: MIT
*   Project: https://github.com/rclone/rclone
