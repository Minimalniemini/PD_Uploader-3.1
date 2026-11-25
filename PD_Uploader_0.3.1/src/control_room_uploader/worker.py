"""Permanent browser worker, independent of the GUI toolkit."""
from queue import Empty

class BrowserWorker:
    def __init__(self, tasks, events, engine, pool, log):
        self.tasks, self.events, self.engine, self.pool, self.log = tasks, events, engine, pool, log

    def run(self):
        while True:
            try:
                action = self.tasks.get(timeout=.15)
            except Empty:
                for session in tuple(self.engine.sessions.values()):
                    session.pump()
                continue
            if action is None:
                try:
                    self.engine.close_sessions()
                    self.pool.close()
                finally:
                    self.events.put(('closed', None))
                return
            try:
                action()
            except Exception as exc:
                self.log(f'{type(exc).__name__}: {exc}')
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('task_done', None))
