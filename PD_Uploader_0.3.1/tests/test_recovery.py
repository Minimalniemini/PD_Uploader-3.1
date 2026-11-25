from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from control_room_uploader.bot import BotStopped, BotUserError, ControlRoomBot
from control_room_uploader.worker import BrowserWorker


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bot = ControlRoomBot(Path('.'))
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.bot._check_access_denied = Mock()

    def test_server_can_finish_after_many_timeouts_without_restarting_request(self):
        action = Mock(side_effect=[TimeoutError()] * 40 + ['ready'])
        self.assertEqual(self.bot._wait_for_server(self.page, action, 'modal'), 'ready')
        self.assertEqual(action.call_count, 41)
        self.page.reload.assert_not_called()
        self.page.goto.assert_not_called()

    def test_stop_interrupts_server_wait(self):
        self.bot._stop_event = threading.Event()
        def action():
            self.bot._stop_event.set()
            raise TimeoutError()
        with self.assertRaises(BotStopped):
            self.bot._wait_for_server(self.page, action, 'modal')

    def test_closed_browser_ends_wait_with_reconnect_instruction(self):
        self.page.is_closed.return_value = True
        with self.assertRaisesRegex(BotUserError, 'reconnect'):
            self.bot._wait_for_server(self.page, Mock(), 'modal')

    def test_close_resets_dialog_registration_for_reopening(self):
        self.bot._context = Mock()
        self.bot._playwright = Mock()
        self.bot._dialog_handler_installed = True
        self.bot._dialog_handler_page_ids = {123}
        self.bot.close()
        self.assertFalse(self.bot._dialog_handler_installed)
        self.assertEqual(self.bot._dialog_handler_page_ids, set())
        self.assertIsNone(self.bot._context)
        self.assertIsNone(self.bot._playwright)

    def test_upload_click_failure_is_never_retried(self):
        root = Mock()
        root.get_by_role.return_value.first.click.side_effect = TimeoutError()
        with self.assertRaisesRegex(BotUserError, 'uncertain'):
            self.bot._click_upload(root, self.page, 2)
        root.get_by_role.return_value.first.click.assert_called_once()
        root.locator.assert_not_called()

    def test_worker_survives_error_and_keeps_browser_on_same_thread(self):
        app = BrowserWorker(queue.Queue(), queue.Queue(), Mock(), Mock(), Mock())
        threads = []
        def failing():
            threads.append(threading.get_ident())
            raise BotUserError('wrong file')
        app.tasks.put(failing)
        app.tasks.put(lambda: threads.append(threading.get_ident()))
        app.tasks.put(None)
        worker = threading.Thread(target=app.run)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        self.assertEqual([e[0] for e in events], ['error', 'task_done', 'task_done', 'closed'])
        app.engine.close_sessions.assert_called_once()
        app.pool.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
