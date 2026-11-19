"""The main window's status bar, which keeps a long message's start and end.

A status message starts with what happened and ends with what was added to
it: the cameras a solve placed unreliably, the cameras that put the floor,
the notes in session.log, an acquisition's warnings. Between them there is
often a full path. QStatusBar cuts a message at its right edge, so in a narrow
window the end goes first. This bar elides the middle instead.
"""
from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import QSizeGrip, QStatusBar, QStyle, QStyleOption


class StatusBar(QStatusBar):
    """A QStatusBar that draws a message too wide for it with its middle
    elided, and shows the whole message as its tooltip.

    Only the drawing changes: ``currentMessage()`` and ``messageChanged``
    carry the message whole. A message that fits is drawn by QStatusBar
    itself. The bar holds messages only: when it elides, it draws no frames
    around widgets added to it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.messageChanged.connect(self._update_tooltip)

    def message_rect(self) -> QRect:
        """Where QStatusBar draws its message: from 6 px in on the left to
        the size grip, or to 12 px in on the right without one."""
        right = self.width() - 12
        for grip in self.findChildren(QSizeGrip):
            if grip.isVisible():
                right = min(right, grip.x())
        return QRect(6, 0, max(0, right - 6), self.height())

    def shown_text(self) -> str:
        """The message as drawn: whole, or elided in the middle to fit."""
        return self.fontMetrics().elidedText(
            self.currentMessage(), Qt.ElideMiddle, self.message_rect().width())

    def _update_tooltip(self, *_):
        text = self.currentMessage()
        self.setToolTip(text if text and self.shown_text() != text else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_tooltip()

    def paintEvent(self, event):
        text = self.currentMessage()
        shown = self.shown_text()
        if shown == text:
            super().paintEvent(event)
            return
        p = QPainter(self)
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_PanelStatusBar, opt, p, self)
        p.setPen(self.palette().windowText().color())
        p.drawText(self.message_rect(),
                   Qt.AlignLeading | Qt.AlignVCenter | Qt.TextSingleLine, shown)
        p.end()
