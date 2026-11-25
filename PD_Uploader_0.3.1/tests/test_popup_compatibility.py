import unittest
import threading
from unittest.mock import Mock
from control_room_uploader.engine import QueueEngine
from control_room_uploader.sessions import ManagedSession
from test_queue import job, FakeSession


class PopupCompatibilityTests(unittest.TestCase):
    def test_accepted_popup_advances_to_next_row_without_a_wording_check(self):
        a = job(counts=(1, 1))
        calls = []
        adapter = object.__new__(ManagedSession)
        adapter.engine = Mock()
        adapter.engine.shutdown = threading.Event()
        adapter._dialog_handler_page_ids = set()
        adapter.awaiting_dialog = True
        adapter.feedback = []
        adapter.log = Mock()
        page = Mock()
        page.is_closed.return_value = False
        adapter._install_page_dialog_handler(page)
        handler = page.on.call_args.args[1]
        accepted = []

        def upload(session, row):
            dialog = Mock()
            dialog.message = 'OK' if row.row_number == 2 else 'Processing complete.'
            handler(dialog)
            dialog.accept.assert_called_once()
            adapter._wait_for_upload_feedback(page, 0)
            accepted.append(row.row_number)

        engine = QueueEngine(lambda j, e: FakeSession(j, e, calls, upload))
        engine.add(a)
        engine.run()
        self.assertEqual(accepted, [2, 3])
        self.assertEqual(calls, [(a.id, 2), (a.id, 3)])
        self.assertEqual(engine.phase, 'finished')
        self.assertEqual(engine.snapshot()['done'], 2)
        self.assertTrue(all(r.status == 'Uploaded' for r in a.rows))


if __name__ == '__main__':
    unittest.main()
