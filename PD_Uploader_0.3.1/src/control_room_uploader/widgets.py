"""Flat, explicitly painted controls matching the approved prototype."""
from math import cos, sin, radians
from PySide6.QtCore import Qt, QRectF, Signal, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QPainterPath
from PySide6.QtWidgets import QWidget, QPushButton, QHeaderView, QStyledItemDelegate, QStyle

TEXT='#20242B'; MUTED='#737983'; YELLOW='#FFCC00'; GREEN='#25834C'; TINT='#FFF2BD'


def draw_checkbox(painter, rect, checked, partial=False, enabled=True):
    box=QRectF(rect.center().x()-6,rect.center().y()-6,12,12)
    painter.save();painter.setRenderHint(QPainter.Antialiasing)
    color=QColor('#087BF5' if checked or partial else '#FFFFFF')
    if not enabled:color.setAlpha(135 if checked or partial else 255)
    painter.setPen(QPen(color if checked or partial else QColor('#BDC2CA'),1))
    painter.setBrush(color);painter.drawRoundedRect(box,2,2)
    if checked or partial:
        painter.setPen(QPen(QColor('white'),1.6,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        path=QPainterPath()
        if partial:
            path.moveTo(box.left()+3,box.center().y());path.lineTo(box.right()-3,box.center().y())
        else:
            path.moveTo(box.left()+2.5,box.top()+6);path.lineTo(box.left()+5,box.top()+8.5);path.lineTo(box.left()+9.5,box.top()+3.5)
        painter.drawPath(path)
    painter.restore()

class ProgressRing(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);self.setFixedSize(150,150);self.percent=0;self.complete=False
        self.setAccessibleName('Upload progress')
    def set_progress(self,percent,complete):
        self.percent,self.complete=percent,complete
        self.setAccessibleDescription(f'{percent}%'+(' complete' if complete else ''));self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        circle=QRectF(12,12,126,126)
        p.setPen(QPen(QColor('#E3E5E8'),7));p.drawEllipse(circle)
        pen=QPen(QColor(GREEN if self.complete else YELLOW),7,Qt.SolidLine,Qt.RoundCap)
        p.setPen(pen)
        if self.complete:p.drawEllipse(circle)
        elif self.percent:p.drawArc(circle,90*16,-int(self.percent*3.6*16))
        else:
            p.setBrush(QColor(YELLOW));p.setPen(Qt.NoPen);p.drawEllipse(QPointF(75,12),3.5,3.5)
        font=QFont('Segoe UI');font.setPixelSize(34);font.setWeight(QFont.DemiBold)
        p.setFont(font);p.setPen(QColor(GREEN if self.complete else TEXT))
        p.drawText(self.rect(),Qt.AlignCenter,'✓' if self.complete else f'{self.percent}%')

class TabButton(QPushButton):
    def __init__(self,text,parent=None):
        super().__init__(text,parent);self.uploading=False
    def paintEvent(self,event):
        super().paintEvent(event)
        if self.uploading:
            p=QPainter(self);p.fillRect(0,self.height()-3,self.width(),3,QColor(YELLOW))

class QueueHeader(QHeaderView):
    toggle_all=Signal()
    def __init__(self,parent=None):
        super().__init__(Qt.Horizontal,parent);self.checked=False;self.partial=False;self.select_enabled=True
        self.setHighlightSections(False);self.setDefaultAlignment(Qt.AlignLeft|Qt.AlignVCenter)
    def paintSection(self,painter,rect,index):
        if index:
            super().paintSection(painter,rect,index);return
        painter.save();painter.fillRect(rect,QColor('#FAFBFC'))
        painter.setPen(QColor('#ECEEF0'));painter.drawLine(rect.bottomLeft(),rect.bottomRight())
        draw_checkbox(painter,rect,self.checked,self.partial,self.select_enabled);painter.restore()
    def mousePressEvent(self,event):
        if self.logicalIndexAt(event.position().toPoint())==0 and self.select_enabled:
            self.toggle_all.emit();event.accept();return
        super().mousePressEvent(event)

class QueueDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        if index.column()==0:
            painter.save()
            bg=index.data(Qt.BackgroundRole)
            if option.state & QStyle.State_Selected:painter.fillRect(option.rect,QColor('#F2F4F7'))
            elif bg:painter.fillRect(option.rect,bg)
            else:painter.fillRect(option.rect,QColor('white'))
            draw_checkbox(painter,option.rect,index.data(Qt.CheckStateRole) in (Qt.Checked, Qt.Checked.value),None,bool(index.flags() & Qt.ItemIsUserCheckable))
            painter.restore()
        else:
            super().paint(painter,option,index)
        painter.save();painter.setPen(QColor('#ECEEF0'));painter.drawLine(option.rect.bottomLeft(),option.rect.bottomRight());painter.restore()
