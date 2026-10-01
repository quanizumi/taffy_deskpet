"""Windows desktop pet — transparent, always-on-top, interactive."""

from __future__ import annotations

import math
import random
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
    QWidget,
)

from cursor_usage import (
    CursorUsageClient,
    FetchResult,
    QuotaSnapshot,
    alert_percent,
    alert_tier,
    mood_level,
    plan_used_percent,
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

# Speed and amplitude multipliers for calm / uneasy / alarm / panic.
_MOOD_SPEED = (1, 2, 2, 3)
_MOOD_AMP = (1.0, 1.0, 1.8, 2.6)

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


def _format_dollars(value: float) -> str:
    """Format a rolled dollar amount. Two decimals, matching cents / 100."""
    return f"${value:.2f}"


def _format_percent(value: float) -> str:
    """One decimal, or a whole number when the value is already on an integer."""
    nearest = int(value + 0.5) if value >= 0 else int(value - 0.5)
    if abs(value - nearest) < 0.05:
        return f"{nearest}%"
    return f"{value:.1f}%"


def _format_plan_percent(value: float) -> str:
    """Half-up whole percent, so 36.5 shows as 37 like Cursor's own headline."""
    if value < 0:
        return f"{int(value - 0.5)}%"
    return f"{int(value + 0.5)}%"


def _money_from_cents(cents: int) -> str:
    """Exact dollar text from integer cents. Does not round through float."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


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
            "remain": None,
            "auto": None,
            "api": None,
            "plan": None,
        }
        self._roll_target: dict[str, float | None] = dict(self._roll)
        self._fetching = False
        self._last_fetch_started = 0.0
        self._usage_bridge = _UsageBridge(self)
        self._usage_bridge.ready.connect(self._on_usage_ready)
        self._usage = CursorUsageClient()

        self._init_voice()
        self._apply_size()
        self._center_on_screen()
        QTimer.singleShot(200, lambda: self._refresh_usage(True))
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(60_000)
        self._usage_timer.timeout.connect(lambda: self._refresh_usage(True))
        self._usage_timer.start()

    def _on_idle_tick(self) -> None:
        """Advance the laugh loop, the squash, the rolling numbers, and the pop."""
        self._idle_ms += 16
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
            "还剩 $8888.88",
            "套餐已用 100% · $8888.88 / $8888.88",
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
        text_w = max(text_w, QFontMetrics(title).horizontalAdvance("还剩 $8888.88"))
        bw = max(text_w + pad * 2, int(sw * 0.92))
        bar_h = max(body_h, max(6, int(8 * u)))
        bh = pad * 2 + title_h + body_h + bar_h * 2 + body_h + gap * 4
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

    def _jitter(self, sprite_h: int) -> tuple[float, float]:
        """Laugh offset. Higher moods step through the GIF frames faster and farther."""
        level = self._level if self._level >= 0 else 0
        speed = _MOOD_SPEED[level]
        amp = _MOOD_AMP[level]
        idx = (self._idle_ms * speed // JITTER_FRAME_MS) % len(JITTER_DY)
        oy = JITTER_DY[idx] * (sprite_h / 700.0) * amp
        ox = 0.0
        if level >= 3:
            ox = (6.0 if (self._idle_ms // 45) % 2 == 0 else -6.0) * (sprite_h / 700.0)
        return ox, oy

    def paintEvent(self, _event) -> None:  # noqa: N802
        """Draw the quota bubble, then the cutout with the GIF's laugh jitter."""
        layout = self._compute_layout()
        ox, oy = self._jitter(layout.sprite_h)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if layout.bubble is not None:
            self._paint_bubble(painter, layout, ox, oy)
        self._paint_sprite(painter, layout, ox, oy)
        if self._level >= 2:
            self._paint_sweat(painter, layout, ox, oy)
        painter.end()

    def _paint_sprite(self, painter: QPainter, layout: _Layout, ox: float, oy: float) -> None:
        """Draw the cutout. Squash keeps the sprite's bottom edge where it was."""
        top = layout.sprite_y + oy
        bottom = top + layout.sprite_h
        draw_h = max(1, int(round(layout.sprite_h * self._squash)))
        painter.drawPixmap(
            int(round(layout.sprite_x + ox)),
            int(round(bottom - draw_h)),
            layout.sprite_w,
            draw_h,
            self._pixmap_src,
        )

    def _paint_bubble(self, painter: QPainter, layout: _Layout, ox: float, oy: float) -> None:
        """Speech bubble above the head: remaining dollars, two bars, one line."""
        assert layout.bubble is not None
        rect = layout.bubble.translated(ox, oy)
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

        remain = self._roll["remain"]
        title = "还剩 —" if remain is None else "还剩 " + _format_dollars(remain)
        painter.setFont(title_font)
        painter.setPen(_INK)
        painter.drawText(QRectF(inner.left(), y, inner.width(), title_h), Qt.AlignmentFlag.AlignCenter, title)
        y += title_h + gap

        painter.setFont(body_font)
        painter.setPen(_INK)
        subtitle = QFontMetrics(body_font).elidedText(
            self._subtitle(), Qt.TextElideMode.ElideRight, int(inner.width())
        )
        painter.drawText(
            QRectF(inner.left(), y, inner.width(), body_h),
            Qt.AlignmentFlag.AlignCenter,
            subtitle,
        )
        y += body_h + gap

        bar_h = max(body_h, max(6, int(8 * u)))
        self._paint_meter(painter, inner.left(), y, inner.width(), bar_h, "Auto", self._roll["auto"], _AUTO_COLOR, body_font)
        y += bar_h + gap
        self._paint_meter(painter, inner.left(), y, inner.width(), bar_h, "API", self._roll["api"], _API_COLOR, body_font)
        y += bar_h + gap

        quote = self._quote()
        painter.setFont(body_font)
        painter.setPen(quote_color)
        elided = QFontMetrics(body_font).elidedText(quote, Qt.TextElideMode.ElideRight, int(inner.width()))
        painter.drawText(QRectF(inner.left(), y, inner.width(), body_h), Qt.AlignmentFlag.AlignCenter, elided)

    def _is_sparse(self) -> bool:
        """True when this reading has neither dollars nor either usage percent."""
        snap = self._snapshot
        if snap is None:
            return True
        if snap.remaining_cents is not None or snap.auto_percent is not None or snap.api_percent is not None:
            return False
        return plan_used_percent(snap) is None

    def _quote(self) -> str:
        """Line under the bars. A missing-field note wins until the pet is clicked."""
        if self._quote_override:
            return self._quote_override
        snap = self._snapshot
        if snap and snap.missing_note and not self._dismiss_note:
            return snap.missing_note
        return self._line

    def _subtitle(self) -> str:
        """Included-plan dollars only. Does not invent a ratio when limit is missing."""
        snap = self._snapshot
        if snap is None:
            return ""
        plan = self._roll["plan"]
        if (
            plan is None
            or snap.included_spend_cents is None
            or snap.limit_cents is None
        ):
            return ""
        return (
            "套餐已用 "
            + _format_plan_percent(plan)
            + " · "
            + _money_from_cents(snap.included_spend_cents)
            + " / "
            + _money_from_cents(snap.limit_cents)
        )

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
        label_w = fm.horizontalAdvance("Auto") + 6
        value = "—" if percent is None else _format_percent(percent)
        value_w = fm.horizontalAdvance("100.0%") + 4
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

    def _paint_sweat(self, painter: QPainter, layout: _Layout, ox: float, oy: float) -> None:
        """A drop on the hair when the quota is in the alarm or panic band."""
        bob = math.sin(self._idle_ms / 180.0) * (3.0 * layout.u)
        cx = layout.sprite_x + layout.sprite_w * 0.78 + ox
        cy = layout.sprite_y + layout.sprite_h * 0.30 + oy + bob
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
            if not was_drag:
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

        top_act = QAction("取消置顶" if self._always_on_top else "始终置顶", self)
        top_act.triggered.connect(self._toggle_topmost)
        menu.addAction(top_act)

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
        self._move_sprite_anchor(anchor_x, anchor_y, "center")

    def _toggle_bubble(self) -> None:
        """Show or hide the quota bubble without moving the character's feet."""
        layout = self._compute_layout()
        anchor_x = self.x() + layout.sprite_x + layout.sprite_w / 2
        anchor_y = self.y() + layout.sprite_y + layout.sprite_h
        self._show_bubble = not self._show_bubble
        self._apply_size()
        self._move_sprite_anchor(anchor_x, anchor_y, "bottom")

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
        """Roll the remaining dollars. Percents snap on first sight, then ease when they change."""
        remain = None if snapshot.remaining_cents is None else snapshot.remaining_cents / 100.0
        self._roll_target["remain"] = remain
        if remain is None:
            self._roll["remain"] = None
        elif self._roll["remain"] is None:
            self._roll["remain"] = 0.0
        for key, value in (
            ("auto", snapshot.auto_percent),
            ("api", snapshot.api_percent),
            ("plan", plan_used_percent(snapshot)),
        ):
            self._roll_target[key] = value
            if value is None or self._roll[key] is None:
                self._roll[key] = value

    def _sync_mood(self, snapshot: QuotaSnapshot) -> None:
        """Update the reaction. Crossing 80 or 100 speaks once; panic sticks until a click."""
        alert = alert_percent(snapshot)
        level = mood_level(alert)
        tier = alert_tier(alert)
        if alert is None and snapshot.remaining_cents is None:
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
    app.setQuitOnLastWindowClosed(True)
    pet = DeskPet()
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
