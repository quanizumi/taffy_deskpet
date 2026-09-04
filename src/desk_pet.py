"""Windows desktop pet — transparent, always-on-top, interactive."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QWidget,
)


def resource_path(relative: str) -> Path:
    """Resolve asset path for both script and PyInstaller frozen EXE."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative


# Vertical offsets sampled from GIF.gif (10fps, no interpolation).
JITTER_DY = (
    0, -15, -3, -15, -7, -15, -7, -3, -7, -11, -7, -3, -15, -7, -11, -12,
    -3, -7, -3, -15, -15, -11, -12, -3, -12, 0, -12, 0, -11, 0, -11, -12,
    -11, -12, -11, -12, -3, -12, 0, -15, 0, -11, -7, -11, -12, -3, -12, 0,
    -15, 0, -11, -7, -11, -12, -3, -12, 0, -11, 0, -11, -12, -11, -12, 0,
    -12, 0, -15, 0, -11, -7, -11, -12, -3, -12, -3, -15, 0, -11, -7, -11,
    -7, -3, -12, -3, -15, 0, -11, -7, -11, -7, -3, -7, -3, -15, -3, -12,
    -3, -15, -7, -3, -7, -3, -15, -3, -15, -7, -15, -7, -3, -7, -3, -15,
    -3, -12, -3, -15, 0, -15, -7, -11, -7, -11, -12, -3, -12, 0, -15, -7,
    -11, -7, -11, 0, -3, -12, 0, -12, 0, -11, 0, -11, -12, -11, -7, -11,
    -12, -15, -12, 0, -11, 0, -11, -7, -11, -12, -3, -7,
)
JITTER_FRAME_MS = 100


class DeskPet(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("桌宠")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

        asset = resource_path("assets/character_cutout.png")
        self._pixmap_src = QPixmap(str(asset))
        if self._pixmap_src.isNull():
            raise FileNotFoundError(f"找不到角色图片: {asset}")

        self._scale = 0.22
        self._min_scale = 0.10
        self._max_scale = 0.85
        self._always_on_top = True

        self._drag_offset = QPoint()
        self._dragging = False
        self._press_pos = QPoint()
        self._moved = False
        self._click_threshold = 6

        self._idle_ms = 0
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(16)
        self._idle_timer.timeout.connect(self._on_idle_tick)
        self._idle_timer.start()

        self._init_voice()
        self._apply_size()
        self._center_on_screen()
        QTimer.singleShot(0, self._play_voice)

    def _on_idle_tick(self) -> None:
        self._idle_ms += 16
        self.update()

    def _scaled_size(self) -> tuple[int, int]:
        w = max(40, int(self._pixmap_src.width() * self._scale))
        h = max(40, int(self._pixmap_src.height() * self._scale))
        return w, h

    def _apply_size(self) -> None:
        w, h = self._scaled_size()
        pad = int(max(w, h) * 0.28)
        self.resize(w + pad * 2, h + pad * 2)
        self._pad = pad
        self.update()

    def _center_on_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.bottom() - self.height() - 40,
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        """Draw the transparent cutout with the GIF's 10fps vertical laugh jitter."""
        w, h = self._scaled_size()
        idx = (self._idle_ms // JITTER_FRAME_MS) % len(JITTER_DY)
        oy = JITTER_DY[idx] * (h / 700.0)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cx = self.width() / 2
        cy = self.height() / 2
        p.drawPixmap(int(cx - w / 2), int(cy - h / 2 + oy), w, h, self._pixmap_src)
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._moved = False
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            pos = event.globalPosition().toPoint()
            if (pos - self._press_pos).manhattanLength() > self._click_threshold:
                self._moved = True
            self.move(pos - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._moved
            self._dragging = False
            if not was_drag:
                self._on_click()
            event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        old_center = self.frameGeometry().center()
        factor = 1.08 if delta > 0 else 1 / 1.08
        self._scale = max(self._min_scale, min(self._max_scale, self._scale * factor))
        self._apply_size()
        self.move(old_center.x() - self.width() // 2, old_center.y() - self.height() // 2)
        event.accept()

    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#fffaf5;border:1px solid #c8b8a8;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#e8d8c8;}"
        )
        size_menu = menu.addMenu("调整大小")
        for label, scale in (("小", 0.14), ("中", 0.22), ("大", 0.34), ("很大", 0.50)):
            act = QAction(label, self)
            act.triggered.connect(lambda checked=False, s=scale: self._set_scale(s))
            size_menu.addAction(act)

        top_act = QAction("取消置顶" if self._always_on_top else "始终置顶", self)
        top_act.triggered.connect(self._toggle_topmost)
        menu.addAction(top_act)

        stop_act = QAction("关闭音频", self)
        stop_act.setEnabled(
            self._player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        )
        stop_act.triggered.connect(self._stop_voice)
        menu.addAction(stop_act)

        menu.addSeparator()
        quit_act = QAction("退出程序", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)

        menu.exec(global_pos)

    def _set_scale(self, scale: float) -> None:
        center = self.frameGeometry().center()
        self._scale = scale
        self._apply_size()
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def _toggle_topmost(self) -> None:
        self._always_on_top = not self._always_on_top
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.show()
        if self._always_on_top:
            self.raise_()

    def _init_voice(self) -> None:
        """Load the mp3 and loop it for the lifetime of the pet."""
        audio_path = resource_path("assets/9月4日.mp3")
        if not audio_path.is_file():
            raise FileNotFoundError(f"找不到音频: {audio_path}")
        self._audio_output = QAudioOutput(self)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setSource(QUrl.fromLocalFile(str(audio_path)))
        self._player.setLoops(QMediaPlayer.Loops.Infinite)

    def _play_voice(self) -> None:
        """Start looping audio; do not restart if it is already playing."""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        self._player.setPosition(0)
        self._player.play()

    def _stop_voice(self) -> None:
        """Stop the click audio from the right-click menu."""
        self._player.stop()

    def _on_click(self) -> None:
        """Click resumes voice if it was stopped from the menu."""
        self._play_voice()


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    pet = DeskPet()
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
