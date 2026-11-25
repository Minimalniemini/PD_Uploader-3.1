"""Compact Qt desktop UI, visually based on the approved prototype."""
from __future__ import annotations
import queue
import sys
import threading
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSignalBlocker, QSize
from PySide6.QtGui import QColor, QIcon, QFont, QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QScrollArea, QPlainTextEdit, QFileDialog, QMessageBox,
    QMenu, QSizePolicy, QStackedWidget,
)
from . import __version__
from .diagnostics import Diagnostics, sanitize, picker_directory, remember_directory
from .engine import QueueEngine
from .jobs import Job
from .paths import resource_path
from .sessions import SessionPool
from .widgets import ProgressRing, TabButton, QueueHeader, QueueDelegate
from .worker import BrowserWorker

STYLE='''
QMainWindow, QWidget#workspace, QWidget#footer { background: #F5F6F8; }
QWidget { color: #20242B; font-family: "Segoe UI", "Helvetica Neue", sans-serif; font-size: 13px; }
QFrame#panel { background: #FFFFFF; border: 1px solid #DADCE0; border-radius: 7px; }
QWidget#inside, QScrollArea, QScrollArea > QWidget > QWidget { background: #FFFFFF; border: none; }
QLabel { background: transparent; border: none; }
QLabel#muted { color: #737983; font-size: 12px; }
QLabel#timer { font-size: 35px; font-weight: 600; }
QLabel#warning { color: #9B7000; font-size: 11px; }
QLabel#queueTitle { font-size: 13px; font-weight: 600; }
QPushButton { background: #FFFFFF; border: 1px solid #D9DDE2; border-radius: 5px; padding: 7px 11px; }
QPushButton:hover { background: #F5F6F8; }
QPushButton:focus { border-color: #B5BAC4; }
QPushButton:disabled { color: #9297A0; }
QPushButton#primary { background: #FFCC00; border: none; font-weight: 600; padding: 10px 5px; }
QPushButton#primary:hover { background: #F2C200; }
QPushButton#primary:disabled { background: #F4E9B7; }
QPushButton#primary[running="true"] { background: #343941; color: #FFFFFF; }
QPushButton#primary[running="true"]:hover { background: #262B32; }
QPushButton#secondary, QPushButton#logToggle { background: transparent; border: none; color: #626873; font-size: 12px; padding: 4px 5px; }
QPushButton#secondary:hover, QPushButton#logToggle:hover { color: #20242B; background: #ECEEF2; }
QPushButton#tab { background: #F0F1F3; border: none; border-radius: 5px; padding: 8px 11px; }
QPushButton#tab[selected="true"] { background: #FFF2BD; font-weight: 600; }
QPushButton#tab[complete="true"] { color: #25834C; }
QPushButton#addTab { background: #FFFFFF; border: 1px dashed #C6CAD0; padding: 8px 10px; }
QTableWidget { background: #FFFFFF; alternate-background-color: #FFFFFF; border: none; outline: none; gridline-color: transparent; selection-background-color: #F2F4F7; selection-color: #20242B; }
QTableWidget::item { border: none; padding-left: 9px; padding-right: 7px; }
QHeaderView { background: #FAFBFC; border: none; }
QHeaderView::section { background: #FAFBFC; color: #737983; border: none; border-bottom: 1px solid #ECEEF0; padding: 9px 8px; font-size: 12px; font-weight: 400; }
QTableWidget QTableCornerButton::section { border: none; background: #FAFBFC; }
QScrollBar:vertical { background: #FFFFFF; width: 10px; margin: 3px 1px; border: none; }
QScrollBar::handle:vertical { background: #96999E; border-radius: 4px; min-height: 28px; margin: 1px; }
QScrollBar:horizontal { background: #FFFFFF; height: 7px; border: none; }
QScrollBar::handle:horizontal { background: #C6C9CE; border-radius: 3px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QPlainTextEdit { background: #FFFFFF; color: #343941; border: 1px solid #DFE2E6; border-radius: 4px; font-family: "Consolas", monospace; font-size: 11px; padding: 7px; }
QToolTip { color: #20242B; background: #FFFFFF; border: 1px solid #D9DDE2; padding: 6px; }
QMenu { background: #FFFFFF; border: 1px solid #D9DDE2; padding: 4px; }
QMenu::item { padding: 7px 14px; }
QMenu::item:selected { background: #FFF2BD; }
'''

LOGIN_PHASES={'opening_login','awaiting_login','preparing','reauth_opening','reauth_login','reauth_preparing'}

class UploaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'PD Uploader  |  v{__version__}')
        self.setWindowIcon(QIcon(str(resource_path('assets/pd.ico'))))
        self.resize(1080,550)
        self.setMinimumSize(900,510)
        self.setStyleSheet(STYLE)
        self.events=queue.Queue();self.tasks=queue.Queue()
        self.pool=SessionPool();self.diagnostics=Diagnostics()
        self.engine=QueueEngine(self.pool.factory,lambda s:self.events.put(('state',s)),self._log)
        self.state=self.engine.snapshot();self.state_at=time.monotonic()
        self.view_id=None;self.task_busy=False;self.closing=False;self.finished_shutdown=False
        self.exit_pending=False;self.exit_was_running=False;self.dialog_open=False
        self.activity=[];self.tab_signature=None;self.table_signature=None;self.table_views={};self.table_job_id=None
        self._build()
        self.browser_worker=BrowserWorker(self.tasks,self.events,self.engine,self.pool,self._log)
        self.thread=threading.Thread(target=self.browser_worker.run,daemon=True,name='PD-browser-worker')
        self.thread.start()
        self.poll_timer=QTimer(self);self.poll_timer.timeout.connect(self._poll);self.poll_timer.start(80)
        open_action=QAction(self);open_action.setShortcut(QKeySequence.Open);open_action.triggered.connect(self._add_files);self.addAction(open_action)
        self._render()

    @staticmethod
    def _label(text='',name=None,align=None):
        label=QLabel(text)
        if name:label.setObjectName(name)
        if align:label.setAlignment(align)
        return label

    def _build(self):
        root=QWidget();root.setObjectName('workspace');self.setCentralWidget(root)
        outer=QVBoxLayout(root);outer.setContentsMargins(12,12,12,0);outer.setSpacing(0)
        self.workspace=QWidget();self.workspace.setObjectName('workspace')
        horizontal=QHBoxLayout(self.workspace);horizontal.setContentsMargins(0,0,0,12);horizontal.setSpacing(12)
        outer.addWidget(self.workspace,1)
        monitor=QFrame();monitor.setObjectName('panel');monitor.setFixedWidth(250)
        left=QVBoxLayout(monitor);left.setContentsMargins(17,20,17,12);left.setSpacing(7)
        self.ring=ProgressRing();left.addWidget(self.ring,0,Qt.AlignHCenter)
        self.count=self._label('0 / 0 images','muted',Qt.AlignCenter);left.addWidget(self.count)
        left.addSpacing(9)
        self.timer=self._label('00:00','timer',Qt.AlignCenter);left.addWidget(self.timer)
        left.addWidget(self._label('Elapsed time','muted',Qt.AlignCenter))
        self.board=self._label('','muted',Qt.AlignCenter);left.addWidget(self.board)
        self.exceptions=self._label('','muted',Qt.AlignCenter);self.exceptions.setWordWrap(True);self.exceptions.hide();left.addWidget(self.exceptions)
        left.addStretch(1)
        self.warning=self._label('','warning',Qt.AlignCenter);self.warning.setWordWrap(True);self.warning.setFixedHeight(30);left.addWidget(self.warning)
        self.primary=QPushButton('Open PD Board');self.primary.setObjectName('primary');self.primary.setFixedHeight(41);self.primary.clicked.connect(self._main);left.addWidget(self.primary)
        self.secondary=QWidget();self.secondary.setObjectName('inside');self.secondary.setFixedHeight(48)
        self.secondary_layout=QVBoxLayout(self.secondary);self.secondary_layout.setContentsMargins(0,0,0,0);self.secondary_layout.setSpacing(0);left.addWidget(self.secondary)
        horizontal.addWidget(monitor)
        right=QFrame();right.setObjectName('panel');right_layout=QVBoxLayout(right);right_layout.setContentsMargins(9,9,9,7);right_layout.setSpacing(0)
        self.tabs_scroll=QScrollArea();self.tabs_scroll.setWidgetResizable(True);self.tabs_scroll.setFrameShape(QFrame.NoFrame)
        self.tabs_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.tabs_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded);self.tabs_scroll.setFixedHeight(44)
        self.tabs_widget=QWidget();self.tabs_widget.setObjectName('inside');self.tabs_layout=QHBoxLayout(self.tabs_widget);self.tabs_layout.setContentsMargins(0,0,0,3);self.tabs_layout.setSpacing(4)
        self.tabs_scroll.setWidget(self.tabs_widget);right_layout.addWidget(self.tabs_scroll)
        self.queue_header=QWidget();self.queue_header.setObjectName('inside');qh=QHBoxLayout(self.queue_header);qh.setContentsMargins(4,11,4,10)
        qh.addWidget(self._label('Upload queue','queueTitle'));qh.addStretch();self.job_count=self._label('','muted');qh.addWidget(self.job_count);right_layout.addWidget(self.queue_header)
        self.stack=QStackedWidget();right_layout.addWidget(self.stack,1)
        self.empty=self._label('No CSV file loaded','muted',Qt.AlignCenter);self.stack.addWidget(self.empty)
        self.table=QTableWidget(0,8);self.table.setObjectName('queue');self.stack.addWidget(self.table)
        self.table.setHorizontalHeaderLabels(['','Row','Tier','Department','Shift','Metric','Images','Status'])
        self.header=QueueHeader(self.table);self.table.setHorizontalHeader(self.header)
        self.table.setHorizontalHeaderLabels(['','Row','Tier','Department','Shift','Metric','Images','Status'])
        self.table.setItemDelegate(QueueDelegate(self.table));self.table.setShowGrid(False)
        self.table.verticalHeader().hide();self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False);self.table.setFrameShape(QFrame.NoFrame)
        self.header.setMinimumSectionSize(26)
        widths=[28,37,66,109,73,112,54,111]
        for i,width in enumerate(widths):
            self.header.setSectionResizeMode(i,QHeaderView.Fixed if i in {0,1,2,4,6} else QHeaderView.Stretch)
            self.table.setColumnWidth(i,width)
        self.header.toggle_all.connect(self._select_all)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(lambda *_:self._inspect_row())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu);self.table.customContextMenuRequested.connect(self._row_menu)
        horizontal.addWidget(right,1)
        footer=QWidget();footer.setObjectName('footer');fl=QHBoxLayout(footer);fl.setContentsMargins(0,3,0,7);fl.setSpacing(5)
        self.log_toggle=QPushButton('› Activity log');self.log_toggle.setObjectName('logToggle');self.log_toggle.clicked.connect(self._toggle_log);fl.addWidget(self.log_toggle)
        fl.addStretch(1);self.footer=self._label('Ready','muted');self.footer.setMaximumWidth(720);fl.addWidget(self.footer);outer.addWidget(footer)
        self.log_panel=QWidget();self.log_panel.setObjectName('workspace');ll=QVBoxLayout(self.log_panel);ll.setContentsMargins(0,0,0,10);ll.setSpacing(3)
        buttons=QHBoxLayout();buttons.addStretch()
        for text,handler in [('Copy',self._copy_log),('Export',self._export_log)]:
            button=QPushButton(text);button.setObjectName('secondary');button.clicked.connect(handler);buttons.addWidget(button)
        ll.addLayout(buttons);self.log_text=QPlainTextEdit();self.log_text.setReadOnly(True);self.log_text.setMaximumBlockCount(2000);self.log_text.setFixedHeight(100);ll.addWidget(self.log_text)
        self.log_panel.hide();outer.addWidget(self.log_panel)

    def _log(self,text):
        text=self.diagnostics.write(text);self.events.put(('log',time.strftime('%H:%M:%S')+' | '+text))

    def _submit(self,action):
        if self.task_busy or self.closing:return
        self.task_busy=True;self.tasks.put(action);self._render()

    def _poll(self):
        redraw=False
        while True:
            try:kind,data=self.events.get_nowait()
            except queue.Empty:break
            if kind=='closed':
                self.poll_timer.stop();self.diagnostics.close();self.finished_shutdown=True;self.close();return
            if kind=='task_done':self.task_busy=False;redraw=True
            elif kind=='state':
                self.state,self.state_at=data,time.monotonic()
                try:self.diagnostics.checkpoint(data)
                except OSError:self._log('Could not save the result summary. Results remain in this window.')
                redraw=True
            elif kind=='log':
                self.activity.append(data);self.activity=self.activity[-2000:];self.log_text.appendPlainText(data)
            elif kind=='error':QMessageBox.warning(self,'PD Uploader',sanitize(data))
        if redraw:self._render()
        self._tick_timer()
        if self.exit_pending and self.state['phase'] not in {'running','pausing'} and not self.dialog_open:
            self.exit_pending=False;self._confirm_exit()

    def _view(self):return next((j for j in self.state['jobs'] if j.id==self.view_id),None)

    def _render(self):
        s=self.state;jobs=s['jobs'];phase=s['phase']
        if self.view_id not in {j.id for j in jobs}:self.view_id=jobs[0].id if jobs else None
        locked=s['started'] or phase in LOGIN_PHASES or self.task_busy or self.closing
        self._tabs(jobs,locked);self._rows(locked)
        success=phase=='finished' and s['total']>0 and s['done']==s['total']
        self.ring.set_progress(s['percent'],success);self.count.setText(f"{s['done']} / {s['total']} images")
        required=[j for j in jobs if j.selected_count]
        if len(required)>1:
            position=next((i+1 for i,j in enumerate(required) if j.id==s['current_id']),None)
            self.board.setText(('Queue finished' if phase=='finished' else 'Queue cancelled') if phase in {'finished','cancelled'} else (f'Board {position} of {len(required)}' if s['started'] and position else f'{sum(j.prepared for j in required)} / {len(required)} boards ready'))
        else:self.board.setText('')
        counts=s['counts'];details=' · '.join(f'{counts[k]} {k.lower()}' for k in ['Failed','Skipped','Cancelled','Outcome unknown'] if counts.get(k))
        self.exceptions.setText(details);self.exceptions.setVisible(bool(details))
        pending=next((j for j in required if not j.prepared),None)
        current=next((j for j in jobs if j.id==s['current_id']),None)
        warning=(current.path.name if current else '') if phase in LOGIN_PHASES or phase=='reauth' else ('Login required: '+pending.path.name if pending and not s['started'] else '')
        self.warning.setText(warning);self.warning.setToolTip(warning)
        labels={'empty':'Open PD Board','idle':'Open PD Board','opening_login':'Opening Edge…','awaiting_login':'I’m logged in','preparing':'Preparing board…',
                'ready':'Start all uploads' if len(required)>1 else 'Start upload','running':'Pause upload','pausing':'Pausing…','paused':'Resume upload',
                'reauth':'Open PD Board','reauth_opening':'Opening Edge…','reauth_login':'I’m logged in','reauth_preparing':'Preparing board…',
                'blocked':'Review upload result','finished':'Retry failed' if s['can_retry'] else 'New upload','cancelled':'New upload'}
        self.primary.setText(labels[phase]);self.primary.setProperty('running',phase=='running');self.primary.style().unpolish(self.primary);self.primary.style().polish(self.primary)
        interactive={'running','paused','reauth','reauth_login','blocked'}
        enabled=bool(s['total']) and phase not in {'opening_login','preparing','pausing','reauth_opening','reauth_preparing'} and not self.closing
        if self.task_busy and phase not in interactive:enabled=False
        if phase in {'finished','cancelled'}:enabled=not self.task_busy and not self.closing
        self.primary.setEnabled(enabled)
        while self.secondary_layout.count():
            item=self.secondary_layout.takeAt(0)
            if item.widget():
                item.widget().hide();item.widget().deleteLater()
        def secondary(text,callback):
            button=QPushButton(text);button.setObjectName('secondary');button.clicked.connect(callback);self.secondary_layout.addWidget(button)
        if phase in {'opening_login','awaiting_login','preparing','reauth_login','reauth_preparing'}:secondary('Back',self.engine.back)
        if phase in {'paused','blocked','reauth','reauth_login'}:
            if s['current_id']:secondary('Skip current board',self._skip)
            secondary('Cancel all uploads',self._cancel)
        elif phase=='finished' and s['can_retry']:secondary('New upload',self._new)
        summary={'empty':'Ready','idle':'Awaiting login','opening_login':'Opening Edge','awaiting_login':'Awaiting login','preparing':'Preparing board',
                 'ready':'Ready','running':'Waiting for server' if s['message'].startswith('Waiting for server') else 'Running',
                 'pausing':'Pausing','paused':'Paused','reauth':'Login required','reauth_opening':'Opening Edge','reauth_login':'Awaiting login','reauth_preparing':'Preparing board',
                 'blocked':'Upload result needs checking','finished':'Completed' if success else 'Finished with exceptions','cancelled':'Cancelled'}[phase]
        self.footer.setText(summary);self.footer.setToolTip(s['message'])

    def _tabs(self,jobs,locked):
        sig=(tuple((j.id,j.path.name,j.state) for j in jobs),self.view_id,self.state['current_id'],self.state['phase'],locked)
        if sig==self.tab_signature:return
        self.tab_signature=sig;position=self.tabs_scroll.horizontalScrollBar().value()
        while self.tabs_layout.count():
            item=self.tabs_layout.takeAt(0)
            if item.widget():
                item.widget().hide();item.widget().deleteLater()
        for job in jobs:
            text=self.fontMetrics().elidedText(job.path.name,Qt.ElideRight,175)+(' ✓' if job.state=='Uploaded' else '')
            button=TabButton(text);button.setObjectName('tab');button.setProperty('selected',job.id==self.view_id);button.setProperty('complete',job.state=='Uploaded')
            button.uploading=job.id==self.state['current_id'] and self.state['started']
            button.setToolTip(job.path.name+'\n'+job.state);button.clicked.connect(lambda checked=False,ident=job.id:self._choose_tab(ident))
            button.setContextMenuPolicy(Qt.CustomContextMenu);button.customContextMenuRequested.connect(lambda point,b=button,ident=job.id:self._tab_menu(b,point,ident))
            self.tabs_layout.addWidget(button)
        add=QPushButton('+' if jobs else '+ Open CSV file');add.setObjectName('addTab');add.setToolTip('Open CSV file');add.setEnabled(not locked);add.clicked.connect(self._add_files);self.tabs_layout.addWidget(add)
        self.tabs_layout.addStretch(1);self.tabs_scroll.horizontalScrollBar().setValue(position)

    def _rows(self,locked):
        job=self._view();self.queue_header.setVisible(job is not None);self.stack.setCurrentWidget(self.table if job else self.empty)
        if job is None:self.table_signature=None;return
        sig=(job.id,locked,self.state['phase'],self.state['current_row'],tuple((r.selected,r.status,r.reason) for r in job.rows))
        if sig==self.table_signature:return
        self.table_signature=sig
        if self.table_job_id:self.table_views[self.table_job_id]=(self.table.verticalScrollBar().value(),self.table.horizontalScrollBar().value(),self.table.currentRow())
        self.table_job_id=job.id;v,h,selected=self.table_views.get(job.id,(0,0,-1))
        blocker=QSignalBlocker(self.table);self.table.setRowCount(len(job.rows))
        for i,state in enumerate(job.rows):
            r=state.row;status=state.status if state.selected else 'Excluded'
            if status=='Waiting' and self.state['phase']=='paused' and self.state['current_id']==job.id and self.state['current_row']==r.row_number:status='Paused'
            values=['',str(r.row_number),r.tier,r.department,r.shift,r.metric,str(state.count),'✓ Uploaded' if status=='Uploaded' else status]
            tooltip=state.reason or f'Row {r.row_number}'
            for col,value in enumerate(values):
                item=QTableWidgetItem(value);item.setToolTip(tooltip);item.setData(Qt.UserRole,r.row_number)
                flags=Qt.ItemIsEnabled|Qt.ItemIsSelectable
                if col==0:
                    if not locked and r.enabled and state.count:flags|=Qt.ItemIsUserCheckable
                    item.setCheckState(Qt.Checked if state.selected else Qt.Unchecked)
                item.setFlags(flags)
                if status in {'Uploading','Paused'}:item.setBackground(QColor('#FFF9E2'))
                if col==7:item.setForeground(QColor('#25834C' if status=='Uploaded' else '#C62828' if status in {'Failed','Outcome unknown'} else '#806000' if status in {'Uploading','Paused'} else '#858A92'))
                elif status in {'Excluded','Skipped','Cancelled'}:item.setForeground(QColor('#858A92'))
                self.table.setItem(i,col,item)
        selectable=[r for r in job.rows if r.row.enabled and r.count];selected_count=sum(r.selected for r in selectable)
        self.header.checked=bool(selectable) and selected_count==len(selectable);self.header.partial=0<selected_count<len(selectable);self.header.select_enabled=not locked;self.header.viewport().update()
        self.job_count.setText(f'{job.successful_count} / {job.selected_count} images' if self.state['started'] else f'{job.selected_count} selected')
        if 0<=selected<len(job.rows):self.table.selectRow(selected)
        self.table.verticalScrollBar().setValue(v);self.table.horizontalScrollBar().setValue(h)
        del blocker

    def _tick_timer(self):
        elapsed=self.state['elapsed']+(time.monotonic()-self.state_at if self.state['clock_running'] else 0)
        n=max(0,int(elapsed));self.timer.setText(f'{n//3600:02d}:{n//60%60:02d}:{n%60:02d}' if n>=3600 else f'{n//60:02d}:{n%60:02d}')

    def _choose_tab(self,ident):self.view_id=ident;self._render()

    def _add_files(self):
        if self.state['started'] or self.task_busy or self.state['phase'] in LOGIN_PHASES:return
        paths,_=QFileDialog.getOpenFileNames(self,'Open CSV file',picker_directory(),'CSV files (*.csv)')
        if not paths:return
        try:remember_directory(Path(paths[0]).parent)
        except OSError:self._log('Could not remember the file-picker directory.')
        def load():
            for path in paths:
                try:self.engine.add(Job.load(Path(path)))
                except Exception as exc:self.events.put(('error',sanitize(str(exc))))
        self._submit(load)

    def _main(self):
        phase=self.state['phase']
        if phase in {'idle','empty'}:self._submit(self.engine.open_login)
        elif phase=='awaiting_login':self._submit(self.engine.confirm_login)
        elif phase=='ready':self._submit(self.engine.run)
        elif phase=='running':self.engine.request_pause()
        elif phase=='paused':self.engine.resume()
        elif phase in {'reauth','reauth_login'}:self.engine.request_login_action()
        elif phase=='blocked':
            if self._ask('Verify upload result','Have you checked the assigned board and verified that ALL images in the current row were saved correctly?\n\nChoose No if any result is uncertain.'):
                self.engine.confirm_unknown_saved()
        elif phase=='finished' and self.state['can_retry']:self._submit(lambda:self.engine.run(retry=True))
        elif phase in {'finished','cancelled'}:self._new()

    def _item_changed(self,item):
        job=self._view()
        if job and item.column()==0 and not self.task_busy and not self.state['started']:
            self.engine.select(job.id,item.data(Qt.UserRole),item.checkState()==Qt.Checked)

    def _select_all(self):
        job=self._view()
        if job and self.header.select_enabled:self.engine.select(job.id,None,not self.header.checked)

    def _tab_menu(self,button,point,ident):
        if self.task_busy or self.state['started'] or self.state['phase'] in LOGIN_PHASES:return
        menu=QMenu(self);action=menu.addAction('Remove from queue');action.triggered.connect(lambda:self._submit(lambda:self.engine.remove(ident)));menu.exec(button.mapToGlobal(point))

    def _row_menu(self,point):
        row=self.table.indexAt(point).row()
        if row<0:return
        self.table.selectRow(row);menu=QMenu(self);menu.addAction('Inspect row',self._inspect_row);menu.exec(self.table.viewport().mapToGlobal(point))

    def _inspect_row(self):
        job=self._view();i=self.table.currentRow()
        if job is None or i<0:return
        state=job.rows[i];r=state.row
        lines=[f'Row {r.row_number} — {state.status}',state.reason or 'No diagnostic issues recorded.']
        lines += [f'Image {n}: {p.name if p else "Not set"}' for n,p in enumerate(r.image_paths,1)]
        QMessageBox.information(self,'Row details','\n'.join(lines))

    def _ask(self,title,text):
        return QMessageBox.question(self,title,text,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes
    def _skip(self):
        if self._ask('Skip current board?','Skip the remaining rows of the execution-current board?\nAlready uploaded images will be retained.'):self.engine.request_skip()
    def _cancel(self):
        if self._ask('Cancel all uploads?','Cancel the remaining queue and keep its results?'):self.engine.request_cancel()
    def _new(self):self._submit(self.engine.clear)
    def _toggle_log(self):
        visible=not self.log_panel.isVisible();self.log_panel.setVisible(visible);self.log_toggle.setText(('⌄' if visible else '›')+' Activity log')
    def _sharing_notice(self):
        return self._ask('Review diagnostics before sharing','Diagnostics may contain filenames or operational details. Review them before sharing outside your organization. Continue?')
    def _copy_log(self):
        if self._sharing_notice():QApplication.clipboard().setText('\n'.join(sanitize(line) for line in self.activity))
    def _export_log(self):
        if not self._sharing_notice():return
        path,_=QFileDialog.getSaveFileName(self,'Export diagnostics','PD-Uploader-diagnostics.txt','Text files (*.txt)')
        if path:
            try:Path(path).write_text('\n'.join(sanitize(line) for line in self.activity),encoding='utf-8')
            except OSError:QMessageBox.warning(self,'Export','Could not write the diagnostics file.')

    def closeEvent(self,event):
        if self.finished_shutdown:event.accept();return
        event.ignore()
        if self.closing:return
        if self.exit_pending:
            if self._ask('Exit while waiting?','The server has not confirmed the operation. Closing cannot cancel a server-side save.\n\nExit and retain an unknown result?'):self._shutdown()
            return
        phase=self.state['phase']
        if phase in {'running','pausing'}:
            self.exit_was_running=phase=='running';self.exit_pending=True;self.engine.request_pause()
        elif self.state['started'] and phase in {'paused','blocked','reauth','reauth_login','reauth_opening','reauth_preparing'}:
            self.exit_was_running=False;self._confirm_exit()
        else:self._shutdown()

    def _confirm_exit(self):
        self.dialog_open=True
        box=QMessageBox(self);box.setWindowTitle('Exit PD Uploader?');box.setText('An upload is in progress. Are you sure you want to exit?')
        cancel=box.addButton('Cancel',QMessageBox.RejectRole);exit_button=box.addButton('Exit',QMessageBox.AcceptRole);box.setDefaultButton(cancel);box.exec()
        self.dialog_open=False
        if box.clickedButton()==exit_button:self._shutdown()
        elif self.exit_was_running and self.state['phase']=='paused':self.engine.resume()

    def _shutdown(self):
        self.closing=True;self.engine.shutdown.set();self.engine.interrupt.set();self.engine.request_cancel();self.tasks.put(None)
        self.primary.setEnabled(False);self.footer.setText('Closing managed browser sessions…')


def main():
    smoke='--smoke-test' in sys.argv
    if sys.platform=='win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('PDUploader.0.3.1')
    app=QApplication.instance() or QApplication([a for a in sys.argv if a!='--smoke-test'])
    app.setApplicationName('PD Uploader');app.setApplicationVersion(__version__)
    app.setStyle('Fusion');app.setWindowIcon(QIcon(str(resource_path('assets/pd.ico'))))
    window=UploaderApp();window.show()
    if smoke:QTimer.singleShot(1500,window._shutdown)
    sys.exit(app.exec())
