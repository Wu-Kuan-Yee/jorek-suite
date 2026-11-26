"""Application entry point."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        sys.stderr.write(
            "JOREK Build Studio requires the optional PySide6 GUI dependency.\n"
            "Install it with: python -m pip install 'PySide6>=6.7'\n"
        )
        return 2

    from .main_window import APP_TITLE, MainWindow

    smoke_test = "--smoke-test" in sys.argv[1:]
    app = QApplication([sys.argv[0]] if smoke_test else sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("JOREK")
    app.setStyle("Fusion")
    window = MainWindow()
    if smoke_test:
        window.close()
        return 0
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
