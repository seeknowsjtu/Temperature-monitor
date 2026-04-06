from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication
import pyqtgraph as pg

from ui import MainWindow


def main() -> None:
    pg.setConfigOptions(antialias=True)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
