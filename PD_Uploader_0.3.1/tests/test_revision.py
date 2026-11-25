import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from control_room_uploader.config import ConfigError
from control_room_uploader.engine import QueueEngine
from control_room_uploader.flow import OutcomeUnknown
from control_room_uploader.jobs import Job
from control_room_uploader.sessions import ManagedSession
from test_queue import FakeSession, job, HEADER


class RevisionTests(unittest.TestCase):
    def test_failed_image_returns_after_all_boards_and_success_is_not_repeated(self):
        a, b = job(counts=(1, 1)), job('B.csv', (1,), 10)
        calls = []
        def action(session, row):
            if row.row_number == 2 and session.job.id == a.id and calls.count((a.id, 2)) == 1:
                raise ConfigError('Unreadable image')
        engine = QueueEngine(lambda j, e: FakeSession(j, e, calls, action))
        engine.add(a); engine.add(b); engine.run()
        self.assertEqual(calls, [(a.id, 2), (a.id, 3), (b.id, 2), (a.id, 2)])
        self.assertEqual(engine.snapshot()['done'], 3)

    def test_board_check_runs_only_on_execution_board_changes(self):
        a, b = job(counts=(1, 1)), job('B.csv', (1, 1), 10)
        checks = []
        def factory(j, e):
            session = FakeSession(j, e, [])
            session.activate = lambda row: checks.append(j.id)
            return session
        engine = QueueEngine(factory)
        engine.add(a); engine.add(b); engine.run()
        self.assertEqual(checks, [a.id, b.id])

    def test_bad_file_in_same_row_does_not_resend_successful_slot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            good, missing = root/'good.jpg', root/'later.jpg'
            good.write_bytes(b'\xff\xd8\xfftest')
            csv = root/'A.csv'
            csv.write_text(HEADER+'yes,https://example.invalid/?siteid=1,T,D,Day,M,good.jpg,later.jpg,,,,\n')
            a = Job.load(csv); a.prepared = True
            sent = []
            def action(session, row):
                sent.append(row.image_paths)
                missing.write_bytes(b'\xff\xd8\xfftest')
            engine = QueueEngine(lambda j, e: FakeSession(j, e, [], action))
            engine.add(a); engine.run()
            self.assertEqual(sent[0][:2], (good, None))
            self.assertEqual(sent[1][:2], (None, missing))
            self.assertEqual(engine.snapshot()['done'], 2)
            self.assertEqual(engine.total, 2)

    def test_unknown_can_be_deferred_without_resubmission_or_skipping_board(self):
        a = job(counts=(1, 1)); calls = []
        def action(session, row):
            if row.row_number == 2:
                raise OutcomeUnknown('No confirmation')
        engine = QueueEngine(lambda j, e: FakeSession(j, e, calls, action))
        engine.add(a)
        def emit(state):
            if state['phase'] == 'blocked':
                engine.defer_unknown()
        engine.emit = emit
        engine.run()
        self.assertEqual(calls, [(a.id, 2), (a.id, 3)])
        self.assertEqual(a.rows[0].status, 'Outcome unknown')
        self.assertEqual(a.rows[1].status, 'Uploaded')

    def test_upload_does_not_minimize_or_repeat_target_and_file_checks(self):
        session = object.__new__(ManagedSession)
        session.engine = Mock(); session.login_approved = True
        session._site_images_needs_reset = False
        row = job().rows[0].row; session.target = row.page_url
        page = Mock(); page.url = row.page_url
        session._current_page_for_run = Mock(return_value=page)
        for name in ('minimize_owned_window', '_check_access_denied', '_verify_target',
                     '_open_site_images_panel', '_set_row_fields', '_assign_images',
                     '_click_upload', '_wait_for_site_images_after_upload'):
            setattr(session, name, Mock())
        session.feedback = []
        session.upload(row)
        session.minimize_owned_window.assert_not_called()
        session._verify_target.assert_not_called()
        session.engine.validate_current_images.assert_not_called()
        session._wait_for_site_images_after_upload.assert_called_once_with(page)


if __name__ == '__main__':
    unittest.main()
