from __future__ import annotations
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock
from control_room_uploader.jobs import Job, RowState
from control_room_uploader.engine import QueueEngine
from control_room_uploader.flow import OutcomeUnknown, ReauthenticationNeeded, PreparationInterrupted
from control_room_uploader.models import UploadRow
from control_room_uploader.config import ConfigError
from control_room_uploader.sessions import classify_popup, target_parts, ManagedSession, SessionPool
from control_room_uploader.diagnostics import Diagnostics, sanitize

HEADER = 'enabled,page_url,tier,department,shift,metric,image_1,image_2,image_3,image_4,image_5,image_6\n'

def job(name='A.csv', counts=(1,), offset=0):
    rows=[]
    for i,count in enumerate(counts):
        r=UploadRow(i+2, True, f'https://example.invalid/?siteid={offset+1}', 'T1', 'D', 'Day', f'M{offset+i}', (None,)*6)
        state=RowState(r, True, images={n:'Waiting' for n in range(1,count+1)})
        rows.append(state)
    return Job(Path(name), rows, prepared=True)

class FakeSession:
    def __init__(self, j, e, calls, action=None):
        self.job, self.engine, self.calls, self.action = j,e,calls,action
        self.closed=False
    def prepare(self, row): pass
    def open_login(self, row): pass
    def confirm_login(self, row): pass
    def upload(self, row):
        self.engine.guard_preparation()
        self.engine.claim_submission()
        self.calls.append((self.job.id, row.row_number))
        if self.action: self.action(self,row)
    def pump(self): pass
    def close(self): self.closed=True
    def invalidate(self): pass

class QueueTests(unittest.TestCase):
    def make(self, jobs, action=None):
        calls=[]
        e=QueueEngine(lambda j,e: FakeSession(j,e,calls,action))
        for j in jobs: e.add(j)
        return e,calls
    def background(self,e):
        self.addCleanup(e.shutdown.set)
        self.addCleanup(e.request_cancel)
        t=threading.Thread(target=e.run,daemon=True)
        t.start()
        self.addCleanup(lambda:t.join(2))
        return t
    def wait_phase(self,e,phase):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if e.snapshot()['phase']==phase: return
            time.sleep(.005)
        self.fail(f'Expected {phase}, got {e.snapshot()["phase"]}')
    def test_independent_jobs_with_same_filename_and_frozen_selection(self):
        a,b=job('same.csv',(1,2)),job('same.csv',(3,),10)
        e,calls=self.make([a,b])
        e.select(a.id,3,False)
        self.assertTrue(b.rows[0].selected)
        self.assertNotEqual(a.id,b.id)
        e.run()
        self.assertEqual(calls,[(a.id,2),(b.id,2)])
        self.assertEqual(e.snapshot()['done'],4)
        self.assertEqual(e.total,4)
        e.select(b.id,2,False)
        self.assertTrue(b.rows[0].selected)
    def test_snapshot_and_view_changes_cannot_change_execution(self):
        a,b=job(counts=(1,1)),job('B.csv',(2,),10)
        def action(session,row):
            view=e.snapshot()
            view['jobs'][0].rows.clear()
            view['current_id']=b.id
        e,calls=self.make([a,b],action)
        e.run()
        self.assertEqual(calls,[(a.id,2),(a.id,3),(b.id,2)])
        self.assertEqual(len(a.rows),2)
    def test_pause_settles_inflight_row_before_next_and_resume_has_no_duplicate(self):
        a=job(counts=(2,1))
        entered, release=threading.Event(),threading.Event()
        def action(session,row):
            if row.row_number==2:
                entered.set()
                release.wait(2)
        e,calls=self.make([a],action)
        t=self.background(e)
        self.assertTrue(entered.wait(1))
        e.request_pause()
        self.assertEqual(e.phase,'pausing')
        self.assertEqual(e.snapshot()['done'],0)
        self.assertEqual(len(calls),1)
        release.set()
        self.wait_phase(e,'paused')
        self.assertEqual(e.snapshot()['done'],2)
        elapsed=e.snapshot()['elapsed'];time.sleep(.03)
        self.assertAlmostEqual(elapsed,e.snapshot()['elapsed'],places=3)
        e.resume();t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(calls,[(a.id,2),(a.id,3)])
        self.assertEqual(e.snapshot()['percent'],100)
    def test_skip_targets_execution_job_not_view_and_preserves_success(self):
        a,b=job(counts=(1,2)),job('B.csv',(3,),10)
        def action(session,row):
            if session.job.id==a.id: session.engine.request_pause()
        e,calls=self.make([a,b],action)
        t=self.background(e);self.wait_phase(e,'paused')
        e.request_skip();t.join(2)
        self.assertEqual(calls,[(a.id,2),(b.id,2)])
        self.assertEqual(a.rows[1].status,'Skipped')
        self.assertEqual(e.snapshot()['done'],4)
        self.assertEqual(e.total,6)
        self.assertEqual(e.snapshot()['percent'],66)
    def test_cancel_waits_for_inflight_confirmation_then_cancels_remaining(self):
        a,b=job(counts=(1,1)),job('B.csv',(1,),10)
        entered,release=threading.Event(),threading.Event()
        def action(s,r): entered.set();release.wait(2)
        e,calls=self.make([a,b],action)
        t=self.background(e);self.assertTrue(entered.wait(1));e.request_cancel()
        self.assertEqual(a.rows[0].status,'Uploading')
        release.set();t.join(2)
        self.assertEqual(a.rows[0].status,'Uploaded')
        self.assertEqual(a.rows[1].status,'Cancelled')
        self.assertEqual(b.rows[0].status,'Cancelled')
        self.assertEqual(len(calls),1)
        self.assertEqual(e.phase,'cancelled')
    def test_cancel_during_last_confirmed_row_finishes_as_complete_success(self):
        a=job()
        def action(session,row):session.engine.request_cancel()
        e,calls=self.make([a],action)
        e.run()
        self.assertEqual(e.phase,'finished')
        self.assertEqual(e.snapshot()['percent'],100)
        self.assertEqual(a.rows[0].status,'Uploaded')
    def test_unknown_never_becomes_retryable_or_success_and_blocks_next(self):
        a,b=job(counts=(1,1)),job('B.csv',(1,),10)
        def action(s,r):
            if s.job.id==a.id: raise OutcomeUnknown('Check board')
        e,calls=self.make([a,b],action)
        t=self.background(e);self.wait_phase(e,'blocked')
        self.assertFalse(e.snapshot()['can_retry'])
        self.assertEqual(len(calls),1)
        self.assertEqual(e.snapshot()['done'],0)
        e.request_skip();t.join(2)
        self.assertEqual(a.rows[0].status,'Outcome unknown')
        self.assertEqual(a.rows[1].status,'Skipped')
        self.assertEqual(b.rows[0].status,'Uploaded')
        self.assertEqual(e.total,3)
    def test_user_verified_unknown_counts_once_and_stays_paused(self):
        a=job(counts=(2,1))
        def action(s,r):
            if r.row_number==2: raise OutcomeUnknown('Check board')
        e,calls=self.make([a],action)
        t=self.background(e);self.wait_phase(e,'blocked')
        e.confirm_unknown_saved();self.wait_phase(e,'paused')
        e.confirm_unknown_saved()
        self.assertEqual(e.snapshot()['done'],2)
        e.resume();t.join(2)
        self.assertEqual(e.snapshot()['done'],3)
        self.assertEqual(len(calls),2)
    def test_reauthentication_keeps_prior_results(self):
        a=job(counts=(1,1));expired=[False]
        def action(s,r):
            if r.row_number==3 and not expired[0]:
                expired[0]=True;raise ReauthenticationNeeded('Login required')
        e,calls=self.make([a],action)
        t=self.background(e);self.wait_phase(e,'reauth')
        self.assertEqual(e.snapshot()['done'],1)
        e.request_login_action();self.wait_phase(e,'reauth_login')
        e.request_login_action();t.join(2)
        self.assertEqual(calls.count((a.id,2)),1)
        self.assertEqual(e.snapshot()['done'],2)
    def test_select_none_job_is_not_executed(self):
        a,b=job(),job('B.csv',(1,),10)
        e,calls=self.make([a,b]);e.select(a.id,None,False);e.run()
        self.assertEqual(calls,[(b.id,2)])
    def test_remove_closes_only_that_jobs_session(self):
        a,b=job(),job('B.csv',(1,),10)
        e,_=self.make([a,b]);sa=e._session(a);sb=e._session(b)
        e.remove(a.id)
        self.assertTrue(sa.closed);self.assertFalse(sb.closed)
    def test_rounding_and_denominator_stay_fixed_after_skip(self):
        a=job(counts=(1,)*124);e,_=self.make([a])
        e.started=True;e.total=124
        for r in a.rows[:106]: r.set_status('Uploaded')
        for r in a.rows[106:]:r.set_status('Skipped')
        s=e.snapshot();self.assertEqual(s['percent'],85);self.assertEqual(s['total'],124)
    def test_double_start_does_not_repeat_finished_run(self):
        a=job();e,calls=self.make([a]);e.run()
        with self.assertRaises(ValueError):e.run()
        self.assertEqual(len(calls),1)
    def test_prepare_back_preserves_other_prepared_session(self):
        a,b=job(),job('B.csv',(1,),10);a.prepared=b.prepared=False
        e,_=self.make([a,b])
        first=e._session(a);second=e._session(b)
        def interrupt(row):e.back();raise PreparationInterrupted()
        second.open_login=interrupt
        e.prepare()
        self.assertEqual(e.phase,"awaiting_login")
        self.assertFalse(a.prepared)
        e.confirm_login()
        e.open_login()
        self.assertTrue(a.prepared);self.assertFalse(b.prepared)
        self.assertFalse(first.closed)

class FileTests(unittest.TestCase):
    def test_load_snapshots_csv_and_diagnoses_missing_without_losing_count(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'A.csv';p.write_text(HEADER+'yes,https://example.invalid/?siteid=1,T,D,Day,Distinct,missing.jpg,,,,,\n')
            j=Job.load(p)
            self.assertEqual(j.selected_count,1)
            p.write_text('changed')
            self.assertEqual(j.rows[0].row.metric,'Distinct')
            with self.assertRaises(ConfigError):j.rows[0].preflight()
            (Path(d)/'missing.jpg').write_bytes(b'\xff\xd8\xfftest')
            j.rows[0].preflight()
    def test_changed_image_is_rejected_before_submit(self):
        with TemporaryDirectory() as d:
            root=Path(d);(root/'one.jpg').write_bytes(b'\xff\xd8\xfftest')
            p=root/'A.csv';p.write_text(HEADER+'yes,https://example.invalid/?siteid=1,T,D,Day,M,one.jpg,,,,,\n')
            j=Job.load(p);(root/'one.jpg').write_bytes(b'changed')
            with self.assertRaises(ConfigError):j.rows[0].preflight()
            engine=QueueEngine(None);engine.add(j);engine.current_id=j.id;engine.current_row=2
            with self.assertRaises(ConfigError):engine.validate_current_images()
    def test_missing_image_safe_retry_preserves_success_and_denominator(self):
        with TemporaryDirectory() as d:
            root=Path(d);(root/'good.jpg').write_bytes(b'\xff\xd8\xfftest')
            p=root/'A.csv';p.write_text(HEADER+'yes,https://example.invalid/?siteid=1,T,D,Day,M,good.jpg,,,,,\nyes,https://example.invalid/?siteid=1,T,D,Day,N,later.jpg,,,,,\n')
            j=Job.load(p);j.prepared=True
            calls=[];e=QueueEngine(lambda j,e:FakeSession(j,e,calls));e.add(j);e.run()
            self.assertEqual(e.snapshot()['done'],1);self.assertTrue(e.snapshot()['can_retry'])
            (root/'later.jpg').write_bytes(b'\xff\xd8\xfftest')
            e.run(retry=True)
            self.assertEqual(calls,[(j.id,2),(j.id,3)])
            self.assertEqual(e.snapshot()['done'],2);self.assertEqual(e.total,2)
    def test_bad_slot_is_deferred_while_good_slot_uploads_once(self):
        with TemporaryDirectory() as d:
            root=Path(d);(root/'good.jpg').write_bytes(b'\xff\xd8\xfftest')
            p=root/'A.csv';p.write_text(HEADER+'yes,https://example.invalid/?siteid=1,T,D,Day,M,good.jpg,missing.jpg,,,,\n')
            j=Job.load(p);j.prepared=True
            calls=[];e=QueueEngine(lambda j,e:FakeSession(j,e,calls));e.add(j);e.run()
            self.assertEqual(calls,[(j.id,2)]);self.assertEqual(e.snapshot()['total'],2)
            self.assertEqual(e.snapshot()['done'],1)
            (root/'missing.jpg').write_bytes(b'\xff\xd8\xfftest')
            e.run(retry=True)
            self.assertEqual(calls,[(j.id,2),(j.id,2)])
            self.assertEqual(e.snapshot()['done'],2)
            self.assertEqual(j.rows[0].status,'Uploaded')
    def test_diagnostics_redacts_addresses_tokens_and_paths_and_journal_has_no_mapping(self):
        self.assertNotIn('example.invalid',sanitize('URL https://example.invalid/?token=abc'))
        self.assertNotIn('abc',sanitize('password=abc token=abc'))
        self.assertNotIn('private-bearer',sanitize('Authorization: Bearer private-bearer'))
        self.assertNotIn('private-json',sanitize('{"token": "private-json"}'))
        self.assertNotIn('private',sanitize(r'C:\private\image.jpg'))
        d=self.enterContext(TemporaryDirectory())
        log=Diagnostics(Path(d));self.addCleanup(log.close)
        e=QueueEngine(None);e.add(job());log.checkpoint(e.snapshot())
        content=(Path(d)/'last-result.json').read_text()
        self.assertNotIn('example.invalid',content);self.assertNotIn('page_url',content)

class ConfirmationTests(unittest.TestCase):
    def test_only_success_text_counts_as_success(self):
        for text in ['Images uploaded successfully','Upload successful','Image has been uploaded.']:
            self.assertEqual(classify_popup(text),'success')
        for text in ['Upload failed','Image not uploaded','Upload unsuccessful','Upload not successful']:
            self.assertEqual(classify_popup(text),'error')
        self.assertEqual(classify_popup('OK'),'unknown')
        self.assertEqual(classify_popup('Please confirm upload'),'unknown')
    def test_slow_confirmation_after_more_than_minute_is_not_retried(self):
        session=object.__new__(ManagedSession)
        session.engine=Mock();session.engine.shutdown=threading.Event()
        session.feedback=[]
        page=Mock();page.is_closed.return_value=False
        ticks=[]
        def wait(ms):
            ticks.append(ms)
            if sum(ticks)>65_000:session.feedback.append('success')
        page.wait_for_timeout.side_effect=wait
        session._wait_for_upload_feedback(page,0)
        self.assertGreater(sum(ticks),60_000)
        page.reload.assert_not_called();page.goto.assert_not_called()
    def test_pause_does_not_abort_inflight_wait(self):
        session=object.__new__(ManagedSession)
        session.engine=Mock();session.engine.shutdown=threading.Event();session.engine.interrupt=threading.Event();session.engine.interrupt.set()
        session.feedback=[];page=Mock();page.is_closed.return_value=False
        page.wait_for_timeout.side_effect=lambda ms:session.feedback.append('success')
        session._wait_for_upload_feedback(page,0)
        page.wait_for_timeout.assert_called_once()
    def test_browser_closed_after_submit_is_unknown(self):
        session=object.__new__(ManagedSession);session.engine=Mock();session.engine.shutdown=threading.Event();session.feedback=[]
        page=Mock();page.is_closed.return_value=True
        with self.assertRaises(OutcomeUnknown):session._wait_for_upload_feedback(page,0)
    def test_target_urls_do_not_allow_embedded_credentials(self):
        from control_room_uploader.flow import TargetMismatch
        with self.assertRaises(TargetMismatch):target_parts('https://user:secret@example.invalid/')
        self.assertEqual(target_parts('https://example.invalid/?siteid=12'),('https://example.invalid','12'))


class AdapterSafetyTests(unittest.TestCase):
    def test_pause_during_login_propagates_without_turning_into_failure(self):
        s=object.__new__(ManagedSession)
        s._page_has_file_inputs=Mock(return_value=False)
        s._addimages_exists=Mock(return_value=False)
        s._wait_for_server=Mock(side_effect=PreparationInterrupted())
        page=Mock()
        with self.assertRaises(PreparationInterrupted):s._ensure_control_room_landing(page)
        page.locator.return_value.first.click.assert_not_called()
    def test_option_wait_can_exceed_minute_and_still_succeed(self):
        s=object.__new__(ManagedSession);s.engine=Mock();s._check_stopped=Mock();s._check_access_denied=Mock()
        s._select_options=Mock(side_effect=[[]]*650+[[{'value':'D1','label':'Wanted'}]])
        page=Mock();page.is_closed.return_value=False
        result=s._wait_for_select_option(page,Mock(),'Wanted','Department',2)
        self.assertEqual(result['value'],'D1')
        self.assertEqual(page.wait_for_timeout.call_count,650)
    def test_job_sessions_have_separate_profiles_and_do_not_stop_shared_runtime(self):
        pool=SessionPool();pool.runtime=Mock()
        e=Mock();e.interrupt=threading.Event()
        a=ManagedSession('a',e,pool);b=ManagedSession('b',e,pool)
        self.addCleanup(a.close);self.addCleanup(b.close)
        self.assertNotEqual(a.profile_dir,b.profile_dir)
        a._context=Mock();b._context=Mock()
        a.close()
        b._context.close.assert_not_called()
        pool.runtime.stop.assert_not_called()
    def test_popup_outside_submit_is_not_used_to_confirm_upload(self):
        s=object.__new__(ManagedSession);s._dialog_handler_page_ids=set();s.awaiting_dialog=False;s.feedback=[];s.log=Mock()
        page=Mock();s._install_page_dialog_handler(page)
        handler=page.on.call_args.args[1]
        dialog=Mock();dialog.message='Images uploaded successfully'
        handler(dialog)
        self.assertEqual(s.feedback,[])
        s.awaiting_dialog=True;handler(dialog)
        self.assertEqual(s.feedback,['success'])
    def test_mismatched_board_never_passes_verification(self):
        from control_room_uploader.flow import TargetMismatch
        s=object.__new__(ManagedSession);s.expected_site='1';s._site_value=Mock(return_value='2')
        page=Mock();page.url='https://example.invalid/ControlRoom'
        with self.assertRaises(TargetMismatch):s._verify_target(page,'https://example.invalid')

class RecoveryClockTests(unittest.TestCase):
    def test_reauthentication_timer_stays_frozen_until_manual_confirmation(self):
        e=QueueEngine(None);e.phase='reauth';e._elapsed=7
        e.resume()
        self.assertEqual(e.phase,'reauth')
        self.assertFalse(e.snapshot()['clock_running'])
        e.request_login_action()
        self.assertEqual(e.phase,'reauth_opening')
        self.assertEqual(e.snapshot()['elapsed'],7)

if __name__ == "__main__":
    unittest.main()
