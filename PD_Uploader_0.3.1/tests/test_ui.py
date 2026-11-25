"""Isolated offscreen acceptance tests of the real desktop controls, no Edge or network."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from control_room_uploader.app import UploaderApp
from control_room_uploader.diagnostics import Diagnostics
from test_queue import job, FakeSession

class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([]);cls.qt.setStyle('Fusion')
    def setUp(self):
        directory=self.enterContext(TemporaryDirectory())
        self.diag=Diagnostics(Path(directory));self.addCleanup(self.diag.close)
        self.patch=patch('control_room_uploader.app.Diagnostics',return_value=self.diag);self.patch.start();self.addCleanup(self.patch.stop)
        self.window=UploaderApp();self.window.show();self.qt.processEvents()
        self.addCleanup(self.finish)
    def finish(self):
        self.window._shutdown()
        deadline=time.monotonic()+2
        while not self.window.finished_shutdown and time.monotonic()<deadline:
            self.qt.processEvents();time.sleep(.01)
        self.assertTrue(self.window.finished_shutdown)
    def sync(self):
        self.window._poll();self.qt.processEvents()
    def wait(self,predicate):
        end=time.monotonic()+2
        while time.monotonic()<end:
            self.sync()
            if predicate():return
            time.sleep(.01)
        self.fail('UI did not reach expected state')
    def test_empty_compact_window_and_pd_icon(self):
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertEqual(self.window.primary.text(),'Open PD Board');self.assertFalse(self.window.primary.isEnabled())
        self.assertLessEqual(self.window.width(),1100);self.assertLessEqual(self.window.height(),600)
        self.assertFalse(self.window.table.isVisible());self.assertFalse(self.window.log_panel.isVisible())
    def test_distinct_real_rows_selection_and_tab_clicks(self):
        a,b=job('A.csv',(1,2)),job('B.csv',(3,),10)
        a.rows[0].row=__import__('dataclasses').replace(a.rows[0].row,department='Alpha',metric='Different A')
        b.rows[0].row=__import__('dataclasses').replace(b.rows[0].row,department='Beta',metric='Different B')
        self.window.engine.add(a);self.window.engine.add(b);self.sync()
        self.assertEqual(self.window.table.rowCount(),2);self.assertEqual(self.window.table.item(0,5).text(),'Different A')
        tab=self.window.tabs_layout.itemAt(1).widget();QTest.mouseClick(tab,Qt.LeftButton);self.qt.processEvents()
        self.assertEqual(self.window.table.rowCount(),1);self.assertEqual(self.window.table.item(0,5).text(),'Different B')
        self.assertEqual(self.window.count.text(),'0 / 6 images')
        self.assertEqual(self.window.engine.current_id,None)
        self.window.engine.select(b.id,2,False);self.sync()
        self.window._choose_tab(a.id)
        self.assertTrue(self.window.engine.jobs[0].rows[0].selected)
    def test_single_primary_button_opens_confirms_then_starts(self):
        a=job();a.prepared=False
        calls=[]
        def factory(j,e):
            session=FakeSession(j,e,[])
            session.open_login=lambda r:calls.append('open')
            session.confirm_login=lambda r:calls.append('confirm')
            return session
        self.window.engine.session_factory=factory;self.window.engine.add(a);self.sync()
        button=self.window.primary
        QTest.mouseClick(button,Qt.LeftButton)
        self.wait(lambda:self.window.state['phase']=='awaiting_login' and not self.window.task_busy)
        self.assertIs(button,self.window.primary);self.assertEqual(button.text(),'I’m logged in');self.assertEqual(calls,['open'])
        QTest.mouseClick(button,Qt.LeftButton)
        self.wait(lambda:self.window.state['phase']=='ready' and not self.window.task_busy)
        self.assertIs(button,self.window.primary);self.assertEqual(button.text(),'Start upload');self.assertEqual(calls,['open','confirm'])
    def test_header_partial_and_frozen_selection_while_inspecting_other_job(self):
        a,b=job('A.csv',(1,1)),job('B.csv',(1,),10)
        self.window.engine.add(a);self.window.engine.add(b);self.window.engine.select(a.id,3,False);self.sync()
        self.assertTrue(self.window.header.partial)
        e=self.window.engine;e.started=True;e.total=2;e.current_id=a.id;e.phase='running';e.notify();self.sync()
        self.window._choose_tab(b.id)
        self.assertFalse(self.window.header.select_enabled);self.assertEqual(e.current_id,a.id)
        self.assertEqual(self.window.primary.text(),'Pause upload')
        e.started=False;e.phase='idle'

if __name__=='__main__':unittest.main()
