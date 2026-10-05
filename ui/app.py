"""Desktop app bootstrap: starts (or finds) the background service, connects, shows the window and tray."""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QVBoxLayout, QWidget

from core import secrets
from . import theme
from .api import Api, Connection, EventThread, load_ui_config, save_ui_config, remote_token
from .main_window import MainWindow
from .quick_ask import GlobalHotkey
from .store import Store

SINGLE_KEY = "OpenGrokBotUI"


class ServiceStarter(QThread):
    ready = Signal(str, int)
    failed = Signal(str)

    def __init__(self, restart: bool = False):
        super().__init__()
        self.restart = restart

    def run(self) -> None:
        try:
            import time

            from service import runner
            if self.restart:
                host, port = runner.configured_endpoint()
                try:
                    import httpx
                    httpx.post(f"http://127.0.0.1:{port}/api/service/stop", headers={"Authorization": f"Bearer {runner.service_token()}"}, timeout=5)
                except Exception:
                    pass
                time.sleep(2.0)
            host, port = runner.ensure_running()
            self.ready.emit(host, port)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class Splash(QWidget):
    def __init__(self, text: str):
        super().__init__()
        self.setWindowTitle("OpenGrokBot")
        self.setWindowIcon(theme.app_icon())
        self.setFixedSize(360, 140)
        v = QVBoxLayout(self)
        self.l = QLabel(text)
        self.l.setWordWrap(True)
        v.addWidget(self.l)


class Controller(QObject):
    def __init__(self, app: QApplication, start_hidden: bool):
        super().__init__()
        self.app, self.start_hidden = app, start_hidden
        self.api: Api | None = None
        self.store: Store | None = None
        self.events: EventThread | None = None
        self.window: MainWindow | None = None
        self.splash: Splash | None = None
        self.starter: ServiceStarter | None = None
        self.hotkey = GlobalHotkey()
        self.hotkey.triggered.connect(lambda: self.window and self.window.quick_ask(True))
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._follow_system_theme)
        self._theme_timer.start(30_000)

    def start(self) -> None:
        cfg = load_ui_config()
        if cfg.get("mode") == "remote" and cfg.get("url") and remote_token():
            self.connect(Connection("remote", cfg["url"], remote_token()))
        else:
            self.start_local()

    def start_local(self, restart: bool = False) -> None:
        self.splash = Splash("Restarting the background service…" if restart else "Starting the background service…")
        self.splash.show()
        self.starter = ServiceStarter(restart)
        self.starter.ready.connect(self._local_ready)
        self.starter.failed.connect(self._failed)
        self.starter.start()

    def _failed(self, msg: str) -> None:
        if self.splash:
            self.splash.close()
        QMessageBox.critical(None, "OpenGrokBot", f"Could not start the background service.\n\n{msg}")
        self.app.quit()

    def _local_ready(self, host: str, port: int) -> None:
        from service import runner
        if self.splash:
            self.splash.close()
        self.connect(Connection("local", f"http://{host}:{port}", runner.service_token()))

    def connect(self, conn: Connection) -> None:
        if self.events:
            self.events.stop()
        if self.api is None:
            self.api = Api(conn)
            self.store = Store(self.api)
        else:
            self.api.set_connection(conn)
        self.events = EventThread(self.api)
        self.events.received.connect(self.store.on_event)
        self.events.connection.connect(self.store.set_connected)
        self.events.start()
        try:
            d = self.api.call("GET", "/api/bootstrap")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(None, "OpenGrokBot", f"Could not reach the service at {conn.url}:\n\n{e}")
            if conn.mode == "remote":
                save_ui_config({**load_ui_config(), "mode": "local"})
                self.start_local()
            else:
                self.app.quit()
            return
        self.store.apply_bootstrap(d)
        self.store.connected = True
        theme_name = d["settings"].get("theme", "dark")
        if theme_name != theme.preference():
            theme.set_theme(theme_name)
            self.app.setStyleSheet(theme.qss())
        if self.window is None:
            self.window = self._make_window()
            self.window.start_page()
            self.apply_prefs()
        if not self.start_hidden:
            self.window.show_window()
        self.start_hidden = False

    def _make_window(self) -> MainWindow:
        w = MainWindow(self.api, self.store)
        w.reconnect.connect(self.on_reconnect)
        w.quitRequested.connect(self.on_quit)
        w.themeRequested.connect(self.on_theme)
        w.appPrefsChanged.connect(lambda: self.apply_prefs(announce=True))
        return w

    def apply_prefs(self, announce: bool = False) -> None:
        """Turn the system-wide Quick Ask shortcut on or off to match Settings > App."""
        if load_ui_config().get("quick_hotkey", True):
            ok = self.hotkey.register()
            if announce and not ok and self.window:
                self.window.toast("Ctrl+Alt+Space is already used by another app, so the global shortcut is off. Quick Ask still opens with Ctrl+J in the app.", "warn")
        else:
            self.hotkey.unregister()

    def _follow_system_theme(self) -> None:
        if theme.preference() == "auto" and theme.system_theme() != theme.theme_name():
            self.on_theme("auto")

    def on_theme(self, name: str) -> None:
        """Switch theme live by rebuilding the window (every widget picks up the new palette)."""
        cfg = load_ui_config()
        theme.configure(cfg.get("accent"), cfg.get("density"), cfg.get("text"))
        theme.set_theme(name)
        self.app.setStyleSheet(theme.qss())
        save_ui_config({**cfg, "theme": name})
        old = self.window
        geo = old.geometry() if old else None
        self.window = self._make_window()
        if geo:
            self.window.setGeometry(geo)
        self.window.start_page()
        self.window.show_window()
        self.window.select(old.current_key if old and old.current_key else "page:settings")
        if old:
            old.really_quit = True
            old.hide()
            old.deleteLater()

    def on_reconnect(self, what) -> None:
        if what == "restart":
            if self.api and self.api.conn.mode == "local":
                if self.events:
                    self.events.stop()
                self.start_local(restart=True)
            return
        self.connect(what) if isinstance(what, Connection) and what.mode == "remote" else self.start_local()

    def on_quit(self, stop_service: bool) -> None:
        self.hotkey.unregister()
        if stop_service and self.api and self.api.conn.mode == "local":
            try:
                self.api.call("POST", "/api/service/stop", json_body={}, timeout=5)
            except Exception:
                pass
        if self.events:
            self.events.stop()
        self.app.quit()


def run_ui(start_hidden: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OpenGrokBot")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(theme.app_icon())
    cfg = load_ui_config()
    theme.configure(cfg.get("accent"), cfg.get("density"), cfg.get("text"))
    theme.set_theme(cfg.get("theme", "dark"))
    app.setStyleSheet(theme.qss())

    # one UI per user: a second launch just raises the first
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(500)
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SINGLE_KEY)
    server.listen(SINGLE_KEY)

    ctl = Controller(app, start_hidden)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), ctl.window and ctl.window.show_window()))
    QTimer.singleShot(0, ctl.start)
    code = app.exec()
    server.close()
    return code
