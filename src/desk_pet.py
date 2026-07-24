"""Windows desktop pet — transparent, always-on-top, interactive."""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QParallelAnimationGroup,
    QSequentialAnimationGroup,
    Qt,
    QTimer,
    Property,
    QRectF,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
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


DIALOGUES = [
    "关注塔菲喵",
    "塔不灭",
    "红叶最多情，塔菲不多情",
]


class BubbleLabel(QLabel):
    """Speech bubble that does not cover the pet body."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._font = QFont("Microsoft YaHei UI", 11)
        self._font.setBold(True)
        self.setFont(self._font)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._text = ""
        self._padding_x = 14
        self._padding_y = 10
        self._tail = 8

    def show_text(self, text: str, near: QWidget, duration_ms: int = 2200) -> None:
        self._text = text
        metrics = self.fontMetrics()
        tw = metrics.horizontalAdvance(text)
        th = metrics.height()
        w = tw + self._padding_x * 2 + self._tail * 2
        h = th + self._padding_y * 2
        self.resize(w, h)

        geo = near.frameGeometry()
        screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        sg = screen.availableGeometry() if screen else near.geometry()

        y = geo.top() + int(geo.height() * 0.06)
        x_right = geo.right() + 4
        x_left = geo.left() - w - 4
        if x_right + w <= sg.right():
            x = x_right
            self._tail_side = "left"
        elif x_left >= sg.left():
            x = x_left
            self._tail_side = "right"
        else:
            x = max(sg.left(), min(x_right, sg.right() - w))
            self._tail_side = "left"

        y = max(sg.top(), min(y, sg.bottom() - h))
        self.move(x, y)
        self.show()
        self.raise_()
        self._hide_timer.start(duration_ms)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = getattr(self, "_tail_side", "left")
        t = self._tail
        if side == "left":
            body = QRectF(t, 0, self.width() - t, self.height())
        else:
            body = QRectF(0, 0, self.width() - t, self.height())

        path = QPainterPath()
        path.addRoundedRect(body, 12, 12)
        mid_y = body.center().y()
        if side == "left":
            path.moveTo(body.left() + 2, mid_y - 7)
            path.lineTo(0, mid_y)
            path.lineTo(body.left() + 2, mid_y + 7)
        else:
            path.moveTo(body.right() - 2, mid_y - 7)
            path.lineTo(self.width(), mid_y)
            path.lineTo(body.right() - 2, mid_y + 7)

        p.setPen(QColor(60, 50, 70, 180))
        p.setBrush(QColor(255, 250, 245, 235))
        p.drawPath(path)
        p.setPen(QColor(70, 55, 80))
        p.drawText(body.toRect(), int(Qt.AlignmentFlag.AlignCenter), self._text)
        p.end()


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

        asset = resource_path("assets/character.png")
        self._pixmap_src = QPixmap(str(asset))
        if self._pixmap_src.isNull():
            raise FileNotFoundError(f"找不到角色图片: {asset}")

        self._scale = 0.28
        self._min_scale = 0.12
        self._max_scale = 1.0
        self._always_on_top = True

        self._drag_offset = QPoint()
        self._dragging = False
        self._press_pos = QPoint()
        self._moved = False
        self._click_threshold = 6

        # --- interaction animation state ---
        self._anim_offset_x = 0.0
        self._anim_offset_y = 0.0
        self._anim_sx = 1.0
        self._anim_sy = 1.0
        self._animating = False
        self._interaction_index = 0
        self._last_dialogue: str | None = None
        self._anim_group: QSequentialAnimationGroup | QParallelAnimationGroup | None = None

        # --- idle animation state ---
        self._idle_t = 0.0

        # idle timer: ~60 fps
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(16)
        self._idle_timer.timeout.connect(self._on_idle_tick)
        self._idle_timer.start()

        self._bubble = BubbleLabel()
        self._apply_size()
        self._center_on_screen()

    # ── animatable properties (for click interactions) ──────────────────────
    def get_offset_x(self) -> float:
        return self._anim_offset_x

    def set_offset_x(self, v: float) -> None:
        self._anim_offset_x = v
        self.update()

    def get_offset_y(self) -> float:
        return self._anim_offset_y

    def set_offset_y(self, v: float) -> None:
        self._anim_offset_y = v
        self.update()

    def get_sx(self) -> float:
        return self._anim_sx

    def set_sx(self, v: float) -> None:
        self._anim_sx = v
        self.update()

    def get_sy(self) -> float:
        return self._anim_sy

    def set_sy(self, v: float) -> None:
        self._anim_sy = v
        self.update()

    offset_x = Property(float, get_offset_x, set_offset_x)
    offset_y = Property(float, get_offset_y, set_offset_y)
    sx = Property(float, get_sx, set_sx)
    sy = Property(float, get_sy, set_sy)

    # ── idle tick ─────────────────────────────────────────────────────────────
    def _on_idle_tick(self) -> None:
        self._idle_t += 0.016
        self.update()

    # ── size helpers ──────────────────────────────────────────────────────────
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

    # ── painting ──────────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:  # noqa: N802
        t = self._idle_t
        w, h = self._scaled_size()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Idle: gentle float + breathe + slight whole-body sway
        breath_y = math.sin(t * (2 * math.pi / 3.2)) * 2.5
        breath_sy = 1.0 + math.sin(t * (2 * math.pi / 3.2)) * 0.008
        sway_angle = math.sin(t * (2 * math.pi / 2.4)) * 1.8

        final_ox = self._anim_offset_x
        final_oy = self._anim_offset_y + (0.0 if self._animating else breath_y)
        final_sx = self._anim_sx
        final_sy = self._anim_sy * (1.0 if self._animating else breath_sy)

        cx = self.width() / 2 + final_ox
        cy = self.height() / 2 + final_oy

        p.save()
        p.translate(cx, cy)
        if not self._animating:
            # Pivot near feet so sway looks natural
            p.translate(0, h * 0.38)
            p.rotate(sway_angle)
            p.translate(0, -h * 0.38)
        p.scale(final_sx, final_sy)
        p.drawPixmap(int(-w / 2), int(-h / 2), w, h, self._pixmap_src)
        p.restore()
        p.end()

    # ── mouse ─────────────────────────────────────────────────────────────────
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
            if not was_drag and not self._animating:
                self._trigger_interaction()
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

    # ── right-click menu ──────────────────────────────────────────────────────
    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#fffaf5;border:1px solid #c8b8a8;padding:4px;}"
            "QMenu::item{padding:6px 24px;}"
            "QMenu::item:selected{background:#e8d8c8;}"
        )
        size_menu = menu.addMenu("调整大小")
        for label, scale in (("小", 0.18), ("中", 0.28), ("大", 0.42), ("很大", 0.62)):
            act = QAction(label, self)
            act.triggered.connect(lambda checked=False, s=scale: self._set_scale(s))
            size_menu.addAction(act)

        top_act = QAction("取消置顶" if self._always_on_top else "始终置顶", self)
        top_act.triggered.connect(self._toggle_topmost)
        menu.addAction(top_act)

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

    # ── dialogue ──────────────────────────────────────────────────────────────
    def _show_dialogue(self) -> None:
        choices = [d for d in DIALOGUES if d != self._last_dialogue] or list(DIALOGUES)
        text = random.choice(choices)
        self._last_dialogue = text
        self._bubble.show_text(text, self)

    # ── click interactions ────────────────────────────────────────────────────
    def _trigger_interaction(self) -> None:
        actions = (self._anim_jump, self._anim_squash, self._anim_shake)
        actions[self._interaction_index % len(actions)]()
        self._interaction_index += 1
        self._show_dialogue()

    def _reset_anim_state(self) -> None:
        self._anim_offset_x = 0.0
        self._anim_offset_y = 0.0
        self._anim_sx = 1.0
        self._anim_sy = 1.0
        self._animating = False
        self.update()

    def _run_group(self, group) -> None:
        if self._anim_group is not None:
            self._anim_group.stop()
        self._animating = True
        self._anim_group = group
        group.finished.connect(self._reset_anim_state)
        group.start()

    def _prop_anim(
        self,
        prop: bytes,
        start: float,
        end: float,
        duration: int,
        easing: QEasingCurve.Type = QEasingCurve.Type.InOutQuad,
    ) -> QPropertyAnimation:
        anim = QPropertyAnimation(self, prop)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(duration)
        anim.setEasingCurve(easing)
        return anim

    def _anim_jump(self) -> None:
        seq = QSequentialAnimationGroup(self)
        up = self._prop_anim(b"offset_y", 0.0, -90.0, 280, QEasingCurve.Type.OutCubic)
        down = self._prop_anim(b"offset_y", -90.0, 0.0, 320, QEasingCurve.Type.InCubic)
        land = QParallelAnimationGroup()
        land.addAnimation(self._prop_anim(b"sy", 1.0, 0.88, 90))
        land.addAnimation(self._prop_anim(b"sx", 1.0, 1.08, 90))
        recover = QParallelAnimationGroup()
        recover.addAnimation(self._prop_anim(b"sy", 0.88, 1.0, 120))
        recover.addAnimation(self._prop_anim(b"sx", 1.08, 1.0, 120))
        seq.addAnimation(up)
        seq.addAnimation(down)
        seq.addAnimation(land)
        seq.addAnimation(recover)
        self._run_group(seq)

    def _anim_squash(self) -> None:
        seq = QSequentialAnimationGroup(self)
        squash = QParallelAnimationGroup()
        squash.addAnimation(self._prop_anim(b"sy", 1.0, 0.55, 160, QEasingCurve.Type.OutQuad))
        squash.addAnimation(self._prop_anim(b"sx", 1.0, 1.25, 160, QEasingCurve.Type.OutQuad))
        squash.addAnimation(self._prop_anim(b"offset_y", 0.0, 18.0, 160))
        bounce = QParallelAnimationGroup()
        bounce.addAnimation(self._prop_anim(b"sy", 0.55, 1.12, 180, QEasingCurve.Type.OutBack))
        bounce.addAnimation(self._prop_anim(b"sx", 1.25, 0.92, 180, QEasingCurve.Type.OutBack))
        bounce.addAnimation(self._prop_anim(b"offset_y", 18.0, -8.0, 180))
        settle = QParallelAnimationGroup()
        settle.addAnimation(self._prop_anim(b"sy", 1.12, 1.0, 140))
        settle.addAnimation(self._prop_anim(b"sx", 0.92, 1.0, 140))
        settle.addAnimation(self._prop_anim(b"offset_y", -8.0, 0.0, 140))
        seq.addAnimation(squash)
        seq.addAnimation(bounce)
        seq.addAnimation(settle)
        self._run_group(seq)

    def _anim_shake(self) -> None:
        seq = QSequentialAnimationGroup(self)
        amps = [18, -18, 14, -14, 8, -8, 0]
        prev = 0.0
        for amp in amps:
            seq.addAnimation(
                self._prop_anim(b"offset_x", prev, float(amp), 55, QEasingCurve.Type.InOutSine)
            )
            prev = float(amp)
        self._run_group(seq)


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
