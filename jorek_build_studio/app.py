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

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("JOREK")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
