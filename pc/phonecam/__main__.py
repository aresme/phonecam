from __future__ import annotations

import argparse
import logging
import os
import sys

from phonecam import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phonecam", description="Android phone as a webcam")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"phonecam {__version__}")
    parser.add_argument(
        "--install-desktop",
        action="store_true",
        help="add PhoneCam to the app grid (~/.local/share) and exit",
    )
    parser.add_argument(
        "--uninstall-desktop", action="store_true", help="remove the app-grid entry and exit"
    )
    args, qt_args = parser.parse_known_args(argv)
    if args.install_desktop or args.uninstall_desktop:
        from phonecam import desktop

        paths = desktop.install() if args.install_desktop else desktop.uninstall()
        for path in paths:
            print(("installed " if args.install_desktop else "removed ") + str(path))
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(threadName)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Qt's default gtk3 platform theme aborts on current GNOME (it reads the removed xsettings
    # key 'antialiasing'). Qt's own GNOME theme avoids GTK entirely. Users can still override.
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "gnome")

    # Imported late so --help/--version work without a display.
    from phonecam.app import run

    return run([sys.argv[0], *qt_args])


if __name__ == "__main__":
    sys.exit(main())
