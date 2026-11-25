from pathlib import Path
from unittest.mock import Mock
import unittest
from control_room_uploader.engine import QueueEngine
from control_room_uploader.flow import ReauthenticationNeeded
from control_room_uploader.sessions import ManagedSession
from test_queue import job, FakeSession

class ManualLoginTests(unittest.TestCase):
    def test_open_never_prepares_until_confirmed_and_uses_each_jobs_session(self):
        a,b=job(),job('B.csv',(1,),10);a.prepared=b.prepared=False
        calls=[];sessions={}
        def factory(j,e):
            session=FakeSession(j,e,[])
            session.open_login=Mock(side_effect=lambda row:calls.append(('open',j.id)))
            session.confirm_login=Mock(side_effect=lambda row:calls.append(('confirm',j.id)))
            sessions[j.id]=session;return session
        e=QueueEngine(factory);e.add(a);e.add(b)
        e.open_login()
        self.assertEqual(e.phase,'awaiting_login');self.assertFalse(a.prepared)
        self.assertEqual(calls,[('open',a.id)])
        e.confirm_login()
        self.assertTrue(a.prepared);self.assertFalse(b.prepared)
        self.assertEqual(e.phase,'idle');self.assertIsNone(e.current_id)
        self.assertEqual(calls,[('open',a.id),('confirm',a.id)])
        e.open_login();self.assertEqual(e.phase,'awaiting_login');self.assertEqual(e.current_id,b.id)
        e.confirm_login();self.assertEqual(e.phase,'ready')
        self.assertEqual(calls,[('open',a.id),('confirm',a.id),('open',b.id),('confirm',b.id)])
    def test_back_from_login_does_not_clear_prepared_sessions(self):
        a,b=job(),job('B.csv',(1,),10);b.prepared=False
        e=QueueEngine(lambda j,e:FakeSession(j,e,[]));e.add(a);e.add(b)
        e.open_login();e.back()
        self.assertEqual(e.phase,'idle');self.assertTrue(a.prepared);self.assertEqual(len(e.jobs),2)
    def test_failed_minimize_keeps_login_button_and_never_marks_ready(self):
        a=job();a.prepared=False;s=Mock();s.confirm_login.side_effect=RuntimeError('Could not minimize Edge')
        e=QueueEngine(lambda j,e:s);e.add(a);e.open_login();e.confirm_login()
        self.assertEqual(e.phase,'awaiting_login');self.assertFalse(a.prepared)
    def test_open_login_does_not_click_control_room_or_modal(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s.submitted=False;s.opened_url=None
        s._first_page=Mock();s.restore_owned_window=Mock();s._navigate=Mock()
        s._ensure_control_room_landing=Mock();s._open_site_images_panel=Mock()
        s.open_login(job().rows[0].row)
        s._ensure_control_room_landing.assert_not_called();s._open_site_images_panel.assert_not_called()
        self.assertFalse(s.login_approved)
    def test_explicit_relogin_reopens_csv_login_destination_even_if_url_was_used_before(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s.submitted=False
        row=job().rows[0].row;s.opened_url=row.page_url
        s._first_page=Mock();s.restore_owned_window=Mock();s._navigate=Mock()
        s.open_login(row)
        s._navigate.assert_called_once_with(s._first_page.return_value,row.page_url)
    def test_confirmation_minimizes_before_control_room_and_keeps_same_page(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s.login_approved=False
        page=Mock();s._current_page_for_run=Mock(return_value=page)
        order=[];s.minimize_owned_window=Mock(side_effect=lambda p:order.append(('minimize',p)))
        s.prepare=Mock(side_effect=lambda r:order.append(('prepare',r)))
        row=job().rows[0].row;s.confirm_login(row)
        self.assertEqual(order,[('minimize',page),('prepare',row)]);self.assertTrue(s.login_approved)
    def test_failed_minimize_never_calls_prepare(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s.login_approved=False
        s._current_page_for_run=Mock();s.minimize_owned_window=Mock(side_effect=RuntimeError('Minimize failed'));s.prepare=Mock()
        with self.assertRaises(RuntimeError):s.confirm_login(job().rows[0].row)
        s.prepare.assert_not_called();self.assertFalse(s.login_approved)
    def test_only_owned_window_id_is_minimized_and_verified(self):
        s=object.__new__(ManagedSession);s.engine=Mock()
        client=Mock();calls=[]
        def send(method,params=None):
            calls.append((method,params))
            return {'bounds':{'windowState':'normal' if len(calls)==1 else 'minimized'}}
        client.send.side_effect=send;s._window_session=Mock(return_value=(client,73))
        page=Mock();s.minimize_owned_window(page)
        self.assertEqual(calls[1],('Browser.setWindowBounds',{'windowId':73,'bounds':{'windowState':'minimized'}}))
        self.assertTrue(all(p['windowId']==73 for _,p in calls));client.detach.assert_called_once()
    def test_minimize_failure_stops_instead_of_falling_back_to_visible_preparation(self):
        from control_room_uploader.bot import BotUserError
        s=object.__new__(ManagedSession);s.engine=Mock();s._window_session=Mock(side_effect=RuntimeError())
        with self.assertRaises(BotUserError):s.minimize_owned_window(Mock())
    def test_cannot_prepare_without_manual_confirmation(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s.login_approved=False
        s._ensure_control_room_landing=Mock()
        with self.assertRaises(ReauthenticationNeeded):s.prepare(job().rows[0].row)
        s._ensure_control_room_landing.assert_not_called()

if __name__=='__main__':unittest.main()
