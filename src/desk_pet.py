"""Windows desktop pet that follows the Cursor window."""

from __future__ import annotations

import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

from cursor_host import (
    find_cursor_host,
    login_autostart_enabled,
    place_above,
    set_login_autostart,
    window_rect,
)
from cursor_usage import (
    CursorUsageClient,
    FetchResult,
    QuotaSnapshot,
    alert_percent,
    alert_tier,
    displayed_percent,
    mood_level,
)


def resource_path(relative: str) -> Path:
    """Resolve asset path for both script and PyInstaller frozen EXE."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative


_LINES: dict[int, tuple[str, ...]] = {
    0: (
        "嘿嘿，额度还胖乎乎的！",
        "随便问，塔菲罩着！",
        "今天还能笑很久～",
        "金库还鼓，继续笑！",
    ),
    1: (
        "肚子开始叫了……",
        "贵模型少点哦。",
        "还行，但别挥霍。",
        "塔菲的零食在变少。",
    ),
    2: (
        "再问塔菲要哭了！",
        "额度见底了啊啊啊！",
        "省着点，塔菲求你了！",
    ),
    3: (
        "没了没了没了！",
        "额度光了，塔菲笑不出来……",
        "去充值，不然塔菲罢工！",
    ),
}
_TIER_LINES = {1: _LINES[2][0], 2: _LINES[3][1]}

_FONT_FAMILIES = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "WenQuanYi Micro Hei",
    "sans-serif",
)

# Bubble fill, border, quote color for each mood.
_MOOD_COLORS = (
    (QColor("#fff5f8"), QColor("#f2a0c0"), QColor("#5c3a46")),
    (QColor("#fff8e8"), QColor("#e2b15a"), QColor("#6a4a20")),
    (QColor("#fff1e4"), QColor("#e08040"), QColor("#7a3410")),
    (QColor("#ffe8e8"), QColor("#e05050"), QColor("#8a2030")),
)
_AUTO_COLOR = QColor("#f48cba")
_API_COLOR = QColor("#e0b040")
_INK = QColor("#5a3040")

# Gap from the locked corner of the Cursor window, in physical pixels.
_DEFAULT_MARGIN = 28


class _UsageBridge(QObject):
    """Deliver a background usage result onto the widget thread."""

    ready = Signal(object)


@dataclass
class _Layout:
    """Pixel layout for one paint. Bubble is None when the meter is hidden."""

    win_w: int
    win_h: int
    sprite_x: float
    sprite_y: float
    sprite_w: int
    sprite_h: int
    bubble: QRectF | None
    tail: int
    u: float


def _format_percent(value: float) -> str:
    """Whole percent, the same rounding as Cursor's usage page."""
    return f"{displayed_percent(value)}%"


class DeskPet(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("桌宠")
        self._follow_cursor = True
        self._apply_window_flags()
        self.setMouseTracking(True)

        asset = resource_path("assets/character_cutout.png")
        self._pixmap_src = QPixmap(str(asset))
        if self._pixmap_src.isNull():
            raise FileNotFoundError(f"找不到角色图片: {asset}")

        self._scale = 0.22
        self._min_scale = 0.10
        self._max_scale = 0.85
        # Corner the pet sticks to, and the gap from that corner.
        self._anchor_corner = "br"
        self._anchor_mx = _DEFAULT_MARGIN
        self._anchor_my = _DEFAULT_MARGIN
        self._host = 0

        self._drag_offset = QPoint()
        self._dragging = False
        self._press_pos = QPoint()
        self._moved = False
        self._click_threshold = 6

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(16)
        self._idle_timer.timeout.connect(self._on_idle_tick)
        self._idle_timer.start()

        self._show_bubble = True
        self._snapshot: QuotaSnapshot | None = None
        self._level = -1
        self._tier = 0
        self._sticky = False
        self._line = "塔菲在看额度…"
        self._quote_override: str | None = None
        self._dismiss_note = False
        self._squash = 1.0
        self._squash_target = 1.0
        self._pulse = 0.0
        self._roll: dict[str, float | None] = {
            "auto": None,
            "api": None,
        }
        self._roll_target: dict[str, float | None] = dict(self._roll)
        self._fetching = False
        self._last_fetch_started = 0.0
        self._usage_bridge = _UsageBridge(self)
        self._usage_bridge.ready.connect(self._on_usage_ready)
        self._usage = CursorUsageClient()

        self._init_voice()
        self._apply_size()
        if not self._follow_cursor:
            self._center_on_screen()
        self._init_tray()
        self._host_timer = QTimer(self)
        self._host_timer.setInterval(50)
        self._host_timer.timeout.connect(self._sync_host)
        self._host_timer.start()
        QTimer.singleShot(200, lambda: self._refresh_usage(True))
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(60_000)
        self._usage_timer.timeout.connect(lambda: self._refresh_usage(True))
        self._usage_timer.start()

    def _on_idle_tick(self) -> None:
        """Ease the press squash, the usage numbers, and the bubble pop."""
        self._squash += (self._squash_target - self._squash) * 0.35
        if abs(self._squash - self._squash_target) < 0.004:
            self._squash = self._squash_target
        for key, target in self._roll_target.items():
            current = self._roll[key]
            if target is None or current is None:
                self._roll[key] = target
                continue
            if abs(target - current) < 0.005:
                self._roll[key] = target
            else:
                self._roll[key] = current + (target - current) * 0.22
        if self._pulse > 0:
            self._pulse *= 0.9
            if self._pulse < 0.02:
                self._pulse = 0.0
        if self.isVisible():
            self.update()

    def _scaled_size(self) -> tuple[int, int]:
        w = max(40, int(self._pixmap_src.width() * self._scale))
        h = max(40, int(self._pixmap_src.height() * self._scale))
        return w, h

    def _fonts(self, u: float) -> tuple[QFont, QFont]:
        """Title and body fonts. Pixel size follows the pet scale, with a floor."""
        title = QFont()
        title.setFamilies(list(_FONT_FAMILIES))
        title.setPixelSize(max(11, int(16 * u)))
        title.setBold(True)
        body = QFont()
        body.setFamilies(list(_FONT_FAMILIES))
        body.setPixelSize(max(10, int(12 * u)))
        return title, body

    def _compute_layout(self) -> _Layout:
        """Window and sprite rectangles. Hidden bubble matches the old padded frame."""
        sw, sh = self._scaled_size()
        u = self._scale / 0.22
        if not self._show_bubble:
            pad = int(max(sw, sh) * 0.28)
            return _Layout(sw + pad * 2, sh + pad * 2, pad, pad, sw, sh, None, 0, u)

        title, body = self._fonts(u)
        title_h = QFontMetrics(title).height()
        body_h = QFontMetrics(body).height()
        body_fm = QFontMetrics(body)
        pad = max(8, int(10 * u))
        gap = max(2, int(3 * u))
        samples = [
            "Cursor 100% · 其他 100%",
            "登录过期了，重新打开一下 Cursor",
            "塔菲看走眼了，待会再看",
            self._line,
        ]
        samples.extend(line for pool in _LINES.values() for line in pool)
        if self._quote_override:
            samples.append(self._quote_override)
        if self._snapshot and self._snapshot.missing_note:
            samples.append(self._snapshot.missing_note)
        text_w = max(body_fm.horizontalAdvance(s) for s in samples if s)
        text_w = max(text_w, QFontMetrics(title).horizontalAdvance("Cursor 100% · 其他 100%"))
        bw = max(text_w + pad * 2, int(sw * 0.92))
        bar_h = max(body_h, max(6, int(8 * u)))
        bh = pad * 2 + title_h + bar_h * 2 + body_h + gap * 3
        tail = max(8, int(12 * u))
        top_slack = max(12, int(22 * u))
        side = max(6, int(8 * u))
        win_w = max(sw, bw) + side * 2
        bubble = QRectF((win_w - bw) / 2, top_slack, bw, bh)
        sprite_y = top_slack + bh + tail + max(2, int(4 * u))
        win_h = int(sprite_y + sh + max(8, int(10 * u)))
        return _Layout(
            win_w,
            win_h,
            (win_w - sw) / 2,
            sprite_y,
            sw,
            sh,
            bubble,
            tail,
            u,
        )

    def _apply_size(self) -> None:
        layout = self._compute_layout()
        self.resize(layout.win_w, layout.win_h)
        self.update()

    def _move_sprite_anchor(self, anchor_x: float, anchor_y: float, mode: str) -> None:
        """Keep the sprite's center or bottom-center on the given screen point."""
        layout = self._compute_layout()
        if mode == "bottom":
            y = anchor_y - layout.sprite_y - layout.sprite_h
        else:
            y = anchor_y - layout.sprite_y - layout.sprite_h / 2
        x = anchor_x - layout.sprite_x - layout.sprite_w / 2
        self.move(int(round(x)), int(round(y)))

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
        """Draw the quota bubble, then the still cutout."""
        layout = self._compute_layout()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if layout.bubble is not None:
            self._paint_bubble(painter, layout)
        self._paint_sprite(painter, layout)
        if self._level >= 2:
            self._paint_sweat(painter, layout)
        painter.end()

    def _paint_sprite(self, painter: QPainter, layout: _Layout) -> None:
        """Draw the cutout. Squash keeps the sprite's bottom edge where it was."""
        bottom = layout.sprite_y + layout.sprite_h
        draw_h = max(1, int(round(layout.sprite_h * self._squash)))
        painter.drawPixmap(
            int(round(layout.sprite_x)),
            int(round(bottom - draw_h)),
            layout.sprite_w,
            draw_h,
            self._pixmap_src,
        )

    def _paint_bubble(self, painter: QPainter, layout: _Layout) -> None:
        """Speech bubble above the head: two usage pools, then one line."""
        assert layout.bubble is not None
        rect = layout.bubble
        if rect.top() < 2:
            rect.moveTop(2)
        level = self._level if self._level >= 0 else 0
        fill, border, quote_color = _MOOD_COLORS[level]
        painter.save()
        if self._pulse > 0:
            painter.translate(rect.center().x(), rect.bottom())
            grow = 1.0 + 0.08 * self._pulse
            painter.scale(grow, grow)
            painter.translate(-rect.center().x(), -rect.bottom())

        shadow = rect.translated(0, 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(90, 40, 60, 40))
        painter.drawRoundedRect(shadow, 16 * layout.u, 16 * layout.u)

        radius = max(10.0, 14.0 * layout.u)
        body = QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        tail = QPainterPath()
        cx = rect.center().x()
        tail.moveTo(cx - 10 * layout.u, rect.bottom() - 1)
        tail.lineTo(cx + 10 * layout.u, rect.bottom() - 1)
        tail.lineTo(cx, rect.bottom() + layout.tail)
        tail.closeSubpath()
        painter.setBrush(fill)
        painter.drawPath(body)
        painter.drawPath(tail)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border, max(1.5, 2.0 * layout.u)))
        painter.drawPath(body)
        painter.drawPath(tail)

        gloss = rect.adjusted(10 * layout.u, 5 * layout.u, -10 * layout.u, -rect.height() * 0.62)
        if gloss.height() > 4:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 90))
            painter.drawRoundedRect(gloss, 8 * layout.u, 8 * layout.u)

        self._paint_bubble_text(painter, rect, layout.u, quote_color)
        painter.restore()

    def _paint_bubble_text(
        self,
        painter: QPainter,
        rect: QRectF,
        u: float,
        quote_color: QColor,
    ) -> None:
        """Fill the bubble. A login or loading line is centered when there is no snapshot."""
        title_font, body_font = self._fonts(u)
        pad = max(8, int(10 * u))
        gap = max(2, int(3 * u))
        inner = rect.adjusted(pad, pad, -pad, -pad)
        title_h = QFontMetrics(title_font).height()
        body_h = QFontMetrics(body_font).height()
        y = inner.top()

        if self._snapshot is None or self._is_sparse():
            painter.setFont(title_font)
            painter.setPen(quote_color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._quote())
            return

        title = self._usage_title()
        painter.setFont(title_font)
        painter.setPen(_INK)
        painter.drawText(QRectF(inner.left(), y, inner.width(), title_h), Qt.AlignmentFlag.AlignCenter, title)
        y += title_h + gap

        bar_h = max(body_h, max(6, int(8 * u)))
        self._paint_meter(painter, inner.left(), y, inner.width(), bar_h, "Cursor", self._roll["auto"], _AUTO_COLOR, body_font)
        y += bar_h + gap
        self._paint_meter(painter, inner.left(), y, inner.width(), bar_h, "其他", self._roll["api"], _API_COLOR, body_font)
        y += bar_h + gap

        quote = self._quote()
        painter.setFont(body_font)
        painter.setPen(quote_color)
        elided = QFontMetrics(body_font).elidedText(quote, Qt.TextElideMode.ElideRight, int(inner.width()))
        painter.drawText(QRectF(inner.left(), y, inner.width(), body_h), Qt.AlignmentFlag.AlignCenter, elided)

    def _is_sparse(self) -> bool:
        """True when neither usage-page pool came back."""
        snap = self._snapshot
        if snap is None:
            return True
        return snap.auto_percent is None and snap.api_percent is None

    def _usage_title(self) -> str:
        """Headline matching the usage page: Cursor Models, then Other Models."""
        auto = self._roll["auto"]
        api = self._roll["api"]
        auto_text = "—" if auto is None else _format_percent(auto)
        api_text = "—" if api is None else _format_percent(api)
        return f"Cursor {auto_text} · 其他 {api_text}"

    def _quote(self) -> str:
        """Line under the bars. A missing-field note wins until the pet is clicked."""
        if self._quote_override:
            return self._quote_override
        snap = self._snapshot
        if snap and snap.missing_note and not self._dismiss_note:
            return snap.missing_note
        return self._line

    def _paint_meter(
        self,
        painter: QPainter,
        x: float,
        y: float,
        width: float,
        height: float,
        label: str,
        percent: float | None,
        color: QColor,
        font: QFont,
    ) -> None:
        """One labeled bar. percent None draws an empty track and a dash, not zero."""
        painter.setFont(font)
        fm = QFontMetrics(font)
        label_w = max(fm.horizontalAdvance("Cursor"), fm.horizontalAdvance("其他")) + 6
        value = "—" if percent is None else _format_percent(percent)
        value_w = fm.horizontalAdvance("100%") + 4
        painter.setPen(_INK)
        painter.drawText(QRectF(x, y, label_w, height), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        track_x = x + label_w
        track_w = max(8.0, width - label_w - value_w)
        track_h = max(6.0, height * 0.55)
        track_y = y + (height - track_h) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 170))
        painter.drawRoundedRect(QRectF(track_x, track_y, track_w, track_h), track_h / 2, track_h / 2)
        if percent is not None and percent > 0:
            filled = track_w * max(0.0, min(percent, 100.0)) / 100.0
            if filled > 0.5:
                painter.setBrush(color)
                radius = min(track_h / 2, filled / 2)
                painter.drawRoundedRect(QRectF(track_x, track_y, filled, track_h), radius, radius)
        painter.setPen(_INK)
        painter.drawText(
            QRectF(track_x + track_w, y, value_w, height),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            value,
        )

    def _paint_sweat(self, painter: QPainter, layout: _Layout) -> None:
        """A still drop on the hair when the quota is in the alarm or panic band."""
        cx = layout.sprite_x + layout.sprite_w * 0.78
        cy = layout.sprite_y + layout.sprite_h * 0.30
        rx = max(3.0, 5.0 * layout.u)
        ry = max(4.0, 7.0 * layout.u)
        path = QPainterPath()
        path.moveTo(cx, cy - ry * 1.15)
        path.quadTo(cx + rx * 1.15, cy, cx, cy + ry)
        path.quadTo(cx - rx * 1.15, cy, cx, cy - ry * 1.15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(130, 200, 255, 210))
        painter.drawPath(path)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._squash_target = 0.9
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
            self._squash_target = 1.0
            was_drag = self._moved
            self._dragging = False
            if was_drag and self._follow_cursor:
                self._capture_anchor()
            elif not was_drag:
                self._on_click()
            event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        layout = self._compute_layout()
        anchor_x = self.x() + layout.sprite_x + layout.sprite_w / 2
        anchor_y = self.y() + layout.sprite_y + layout.sprite_h / 2
        factor = 1.08 if delta > 0 else 1 / 1.08
        self._scale = max(self._min_scale, min(self._max_scale, self._scale * factor))
        self._apply_size()
        if self._follow_cursor:
            self._sync_host()
        else:
            self._move_sprite_anchor(anchor_x, anchor_y, "center")
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

        if self._follow_cursor:
            follow_act = QAction("脱离 Cursor，全局置顶", self)
        else:
            follow_act = QAction("回到 Cursor 窗口里", self)
        follow_act.triggered.connect(self._toggle_follow)
        menu.addAction(follow_act)

        auto_act = QAction("取消开机自启" if login_autostart_enabled() else "开机自启", self)
        auto_act.triggered.connect(self._toggle_autostart)
        menu.addAction(auto_act)

        voice_on = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        voice_act = QAction("关闭语音" if voice_on else "开启语音", self)
        voice_act.triggered.connect(self._toggle_voice)
        menu.addAction(voice_act)

        refresh_act = QAction("刷新额度", self)
        refresh_act.triggered.connect(lambda checked=False: self._refresh_usage(True))
        menu.addAction(refresh_act)

        bubble_act = QAction("隐藏额度气泡" if self._show_bubble else "显示额度气泡", self)
        bubble_act.triggered.connect(self._toggle_bubble)
        menu.addAction(bubble_act)

        menu.addSeparator()
        quit_act = QAction("退出程序", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_act)

        menu.exec(global_pos)

    def _set_scale(self, scale: float) -> None:
        layout = self._compute_layout()
        anchor_x = self.x() + layout.sprite_x + layout.sprite_w / 2
        anchor_y = self.y() + layout.sprite_y + layout.sprite_h / 2
        self._scale = scale
        self._apply_size()
        if self._follow_cursor:
            self._sync_host()
        else:
            self._move_sprite_anchor(anchor_x, anchor_y, "center")

    def _toggle_bubble(self) -> None:
        """Show or hide the quota bubble without moving the character's feet."""
        layout = self._compute_layout()
        anchor_x = self.x() + layout.sprite_x + layout.sprite_w / 2
        anchor_y = self.y() + layout.sprite_y + layout.sprite_h
        self._show_bubble = not self._show_bubble
        self._apply_size()
        if self._follow_cursor:
            self._sync_host()
        else:
            self._move_sprite_anchor(anchor_x, anchor_y, "bottom")

    def _apply_window_flags(self) -> None:
        """Frameless tool window. Topmost only in the detached global mode."""
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if not self._follow_cursor:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _toggle_follow(self) -> None:
        """Switch between sticking to Cursor and the old always-on-top desktop mode."""
        self._follow_cursor = not self._follow_cursor
        self._host = 0
        self._apply_window_flags()
        self._tray.setToolTip("永雏塔菲 · 跟着 Cursor" if self._follow_cursor else "永雏塔菲 · 全局置顶")
        if self._follow_cursor:
            self._sync_host()
        else:
            self.show()
            self.raise_()

    def _toggle_autostart(self) -> None:
        """Toggle the Startup shortcut that launches the pet at login."""
        try:
            set_login_autostart(not login_autostart_enabled())
        except (OSError, subprocess.SubprocessError):
            return

    def _init_tray(self) -> None:
        """Tray icon so the pet can be quit while it is waiting for Cursor."""
        icon = QIcon(self._pixmap_src.scaled(
            32,
            32,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("永雏塔菲 · 跟着 Cursor")
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._fill_tray_menu)
        self._tray.setContextMenu(self._tray_menu)
        self._tray.show()

    def _fill_tray_menu(self) -> None:
        """Rebuild the tray menu so the autostart label matches the shortcut."""
        self._tray_menu.clear()
        auto_act = QAction("取消开机自启" if login_autostart_enabled() else "开机自启", self)
        auto_act.triggered.connect(self._toggle_autostart)
        self._tray_menu.addAction(auto_act)
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.quit)
        self._tray_menu.addAction(quit_act)

    def _sync_host(self) -> None:
        """Show the pet on the Agents window, or hide it in the IDE.

        Skipped while dragging so the pointer owns the position. The pet is
        slotted just above Cursor, so other apps still cover it.
        """
        if not self._follow_cursor or self._dragging:
            return
        pet_hwnd = int(self.winId())
        host = find_cursor_host(prefer=self._host, exclude=pet_hwnd)
        if host is None:
            self._host = 0
            if self.isVisible():
                self.hide()
            return
        self._host = host.hwnd
        if not self.isVisible():
            self.show()
            pet_hwnd = int(self.winId())
        rect = window_rect(pet_hwnd)
        if rect is None:
            return
        pet_w = rect[2] - rect[0]
        pet_h = rect[3] - rect[1]
        x, y = self._anchor_xy(host.left, host.top, host.right, host.bottom, pet_w, pet_h)
        place_above(pet_hwnd, host.hwnd, x, y)

    def _anchor_xy(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        pet_w: int,
        pet_h: int,
    ) -> tuple[int, int]:
        """Physical top-left that keeps the saved gap from the locked corner."""
        if self._anchor_corner == "bl":
            x = left + self._anchor_mx
            y = bottom - self._anchor_my - pet_h
        elif self._anchor_corner == "tr":
            x = right - self._anchor_mx - pet_w
            y = top + self._anchor_my
        elif self._anchor_corner == "tl":
            x = left + self._anchor_mx
            y = top + self._anchor_my
        else:
            x = right - self._anchor_mx - pet_w
            y = bottom - self._anchor_my - pet_h
        max_x = right - pet_w
        max_y = bottom - pet_h
        if max_x < left:
            x = left
        else:
            x = min(max(x, left), max_x)
        if max_y < top:
            y = top
        else:
            y = min(max(y, top), max_y)
        return x, y

    def _capture_anchor(self) -> None:
        """Remember which corner the pet was dropped nearest, and the gap."""
        if not self._host:
            return
        host = window_rect(self._host)
        pet = window_rect(int(self.winId()))
        if host is None or pet is None:
            return
        left, top, right, bottom = host
        pet_left, pet_top, pet_right, pet_bottom = pet
        gaps = {
            "tl": (pet_left - left, pet_top - top),
            "tr": (right - pet_right, pet_top - top),
            "bl": (pet_left - left, bottom - pet_bottom),
            "br": (right - pet_right, bottom - pet_bottom),
        }
        corner = min(gaps, key=lambda name: abs(gaps[name][0]) + abs(gaps[name][1]))
        mx, my = gaps[corner]
        self._anchor_corner = corner
        self._anchor_mx = max(0, mx)
        self._anchor_my = max(0, my)

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
        """Start the looping laugh. Does nothing if it is already playing."""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        self._player.setPosition(0)
        self._player.play()

    def _stop_voice(self) -> None:
        """Stop the looping laugh."""
        self._player.stop()

    def _toggle_voice(self) -> None:
        """Right-click voice switch. Startup stays silent until this is turned on."""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._stop_voice()
        else:
            self._play_voice()

    def _on_click(self) -> None:
        """Swap the quote and refresh usage. Voice is only toggled from the menu."""
        if self._snapshot is not None and self._snapshot.missing_note:
            self._dismiss_note = True
        if self._snapshot is not None and self._level >= 0:
            self._sticky = False
            self._pick_line(self._level)
        self._refresh_usage(False)

    def _pick_line(self, level: int) -> None:
        """Choose a quote for this mood, skipping the line already on screen."""
        pool = _LINES[level]
        choices = [line for line in pool if line != self._line]
        self._line = random.choice(choices or pool)

    def _refresh_usage(self, force: bool = False) -> None:
        """Fetch usage on a background thread. Clicks inside 5 seconds do not retry."""
        if self._fetching:
            return
        now = time.monotonic()
        if not force and now - self._last_fetch_started < 5:
            return
        self._fetching = True
        self._last_fetch_started = now
        threading.Thread(target=self._fetch_usage, name="cursor-usage", daemon=True).start()

    def _fetch_usage(self) -> None:
        """Run the network call off the UI thread. The bridge hops the result back."""
        result = self._usage.fetch()
        self._usage_bridge.ready.emit(result)

    def _on_usage_ready(self, result: FetchResult) -> None:
        """Apply a fetch. Transient misses keep the last numbers and change the line."""
        self._fetching = False
        if result.keep_previous:
            self._quote_override = result.message or "塔菲看走眼了，待会再看"
            return
        self._quote_override = None
        self._dismiss_note = False
        if result.snapshot is None:
            self._snapshot = None
            self._level = -1
            self._tier = 0
            self._sticky = False
            self._line = result.message or "没找到 Cursor 登录"
            for key in self._roll:
                self._roll[key] = None
                self._roll_target[key] = None
            return
        self._snapshot = result.snapshot
        self._set_roll_targets(result.snapshot)
        self._sync_mood(result.snapshot)

    def _set_roll_targets(self, snapshot: QuotaSnapshot) -> None:
        """Snap a pool the first time it appears, then ease it when it changes."""
        for key, value in (
            ("auto", snapshot.auto_percent),
            ("api", snapshot.api_percent),
        ):
            self._roll_target[key] = value
            if value is None or self._roll[key] is None:
                self._roll[key] = value

    def _sync_mood(self, snapshot: QuotaSnapshot) -> None:
        """Update the reaction. Crossing 80 or 100 speaks once; panic sticks until a click."""
        alert = alert_percent(snapshot)
        level = mood_level(alert)
        tier = alert_tier(alert)
        if alert is None:
            self._line = snapshot.missing_note or "接口没带回额度"
            self._level = -1
            self._tier = 0
            self._sticky = False
            return
        entered_panic = level >= 3 and self._level < 3
        if tier > self._tier:
            self._pulse = 1.0
        if entered_panic:
            self._line = _TIER_LINES[2]
            self._sticky = True
        elif tier > self._tier and not self._sticky:
            self._line = _TIER_LINES[1]
        elif level < 3 and self._sticky:
            self._sticky = False
            self._pick_line(level)
        elif not self._sticky and level != self._level:
            self._pick_line(level)
        self._level = level
        self._tier = tier


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    pet = DeskPet()
    pet._sync_host()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
