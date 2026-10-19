"""Build a single-file executable with PyInstaller.

    python build_exe.py

Produces `dist/content-search.exe`. Why each flag is needed:

* `pypff`, `PyPDF2` and `win32com` are imported *inside* functions so the app
  starts without them, which also means PyInstaller's static analysis cannot see
  them - they have to be declared.
* `app/static` is data rather than code, so it needs `--add-data`.
* `--noconsole` makes it start silently: no console window, the browser opens on
  its own, and the output the console used to show goes to
  `content-search.log` in the data folder. Stopping it is the "Quit app" button.
* One file is easiest to hand to someone; `--onedir` starts faster because it
  skips unpacking to a temp folder on every launch.
"""

from __future__ import annotations

import os
import sys

import PyInstaller.__main__

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    PyInstaller.__main__.run([
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name", "content-search",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        # The source must be absolute: relative paths in --add-data resolve
        # against the spec folder, not the working directory.
        "--add-data",
        f"{os.path.join(HERE, 'app', 'static')}{os.pathsep}{os.path.join('app', 'static')}",
        # Keep ambient packages out of the bundle. PYTHONPATH in this environment
        # points at unrelated projects, and they were being collected.
        "--exclude-module", "pygame",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--hidden-import", "pypff",
        "--hidden-import", "PyPDF2",
        "--hidden-import", "win32com",
        "--hidden-import", "win32com.client",
        "--hidden-import", "pythoncom",
        "--paths", HERE,
        os.path.join(HERE, "run.py"),
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
