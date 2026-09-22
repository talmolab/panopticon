"""Custom toggle switch widget."""
from PyQt5.QtWidgets import QAbstractButton, QSizePolicy
from PyQt5.QtCore import Qt, QRectF, QSize, pyqtProperty, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QPainter, QColor, QBrush, QPen, QFont


class ToggleSwitch(QAbstractButton):
    def __init__(self, label: str = "", color_on: QColor = QColor(76, 175, 80), parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self._label = label
        self._color_on = color_on
        self._color_off = QColor(80, 80, 100)
        self._thumb_pos = 0.0
        self._anim = QPropertyAnimation(self, b"thumb_pos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _get_thumb_pos(self):
        return self._thumb_pos

    def _set_thumb_pos(self, val):
        self._thumb_pos = val
        self.update()

    thumb_pos = pyqtProperty(float, _get_thumb_pos, _set_thumb_pos)

    # The thumb is driven from the two QAbstractButton virtuals that cover
    # every way the checked state changes, not from the toggled signal:
    #  - checkStateSet() runs for setChecked(), including one made under
    #    blockSignals, so the thumb follows a state a caller sets silently;
    #  - nextCheckState() runs for a user click and the Space key, which flip
    #    the state with the refresh blocked and never reach checkStateSet().
    # Animating from toggled would leave the thumb ON after a silent uncheck;
    # overriding only checkStateSet leaves it OFF after a real click, which is
    # the path the operator uses to arm an acquisition.

    def checkStateSet(self):
        """Animate the thumb after a programmatic setChecked()."""
        self._animate_to(self.isChecked())

    def nextCheckState(self):
        """Animate the thumb after a user click or Space key press."""
        super().nextCheckState()
        self._animate_to(self.isChecked())

    def _animate_to(self, checked: bool):
        """Run the thumb to the given state. A request for a target the thumb
        already rests at, or is already heading to, is a no-op, so a path that
        reaches both virtuals (QAbstractButton.click()) does not restart the
        motion."""
        target = 1.0 if checked else 0.0
        running = self._anim.state() == QPropertyAnimation.Running
        if running and self._anim.endValue() == target:
            return
        if not running and self._thumb_pos == target:
            return
        self._anim.stop()
        self._anim.setStartValue(self._thumb_pos)
        self._anim.setEndValue(target)
        self._anim.start()

    #: Opacity of a disabled switch. The sidebar disables a toggle for sibling
    #: exclusion, encoding, alignment, a solve and busy ops; a switch painted
    #: live in those states invites a click that does nothing, which reads as
    #: a hang. Dimming is the feedback that the control is not available.
    DISABLED_OPACITY = 0.35

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        if not enabled:
            p.setOpacity(self.DISABLED_OPACITY)
        w, h = self.width(), self.height()

        track_h = 22
        track_y = (h - track_h) / 2
        track_w = 44
        label_x = track_w + 12

        color = QColor(
            int(self._color_off.red() + (self._color_on.red() - self._color_off.red()) * self._thumb_pos),
            int(self._color_off.green() + (self._color_on.green() - self._color_off.green()) * self._thumb_pos),
            int(self._color_off.blue() + (self._color_on.blue() - self._color_off.blue()) * self._thumb_pos),
        )

        p.setBrush(QBrush(color))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, track_y, track_w, track_h), track_h / 2, track_h / 2)

        thumb_r = 16
        thumb_x = 3 + self._thumb_pos * (track_w - thumb_r - 6) + thumb_r / 2
        thumb_y = h / 2
        p.setBrush(QBrush(QColor(240, 240, 240)))
        p.drawEllipse(QRectF(thumb_x - thumb_r / 2, thumb_y - thumb_r / 2, thumb_r, thumb_r))

        p.setPen(QPen(QColor(220, 220, 220) if enabled else QColor(150, 150, 160)))
        p.setFont(QFont("Segoe UI", 10))
        p.drawText(int(label_x), 0, w - int(label_x), h, Qt.AlignVCenter | Qt.AlignLeft, self._label)

        p.end()

    def sizeHint(self):
        return QSize(180, 36)
