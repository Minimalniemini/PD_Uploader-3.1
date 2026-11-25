"""Sequential queue. GUI view selection is intentionally absent from this module."""
from __future__ import annotations
import copy
import threading
import time
from collections import Counter
from collections.abc import Callable
from .jobs import Job, RowState
from .config import ConfigError
from .flow import PreparationInterrupted, ReauthenticationNeeded, TargetMismatch, OutcomeUnknown, UploadRejected

class QueueEngine:
    def __init__(self, session_factory: Callable, emit: Callable = lambda _: None, log: Callable = lambda _: None):
        self.session_factory, self.emit, self.log = session_factory, emit, log
        self.jobs: list[Job] = []
        self.sessions: dict[str, object] = {}
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.interrupt = threading.Event()
        self.shutdown = threading.Event()
        self.phase = 'empty'
        self.current_id: str | None = None
        self.current_row: int | None = None
        self.message = ''
        self.total = 0
        self.started = False
        self.pause_requested = False
        self.cancel_requested = False
        self.skip_id: str | None = None
        self.view_job_id: str | None = None
        self.blocked_row: RowState | None = None
        self._elapsed = 0.0
        self._clock: float | None = None
        self._running = False
        self.recovering = False
        self._login_command: str | None = None

    def _time(self) -> float:
        return self._elapsed + (time.monotonic() - self._clock if self._clock is not None else 0)

    def _clock_on(self) -> None:
        if self._clock is None:
            self._clock = time.monotonic()

    def _clock_off(self) -> None:
        self._elapsed = self._time()
        self._clock = None

    def snapshot(self) -> dict:
        with self.lock:
            selected = [r for j in self.jobs for r in j.rows if r.selected]
            done = sum(sum(s == 'Uploaded' for s in r.images.values()) for r in selected)
            total = self.total if self.started else sum(r.count for r in selected)
            counts = Counter(s for r in selected for s in r.images.values())
            return {'jobs': copy.deepcopy(self.jobs), 'phase': self.phase, 'current_id': self.current_id,
                    'current_row': self.current_row, 'message': self.message, 'total': total, 'done': done,
                    'percent': done * 100 // total if total else 0, 'elapsed': self._time(),
                    'clock_running': self._clock is not None, 'counts': dict(counts), 'started': self.started,
                    'can_retry': any(r.status == 'Failed' and r.safe_retry for r in selected)}

    def notify(self) -> None:
        # Keep snapshot and delivery ordered across worker and GUI command threads.
        with self.lock:
            self.emit(self.snapshot())

    def add(self, job: Job) -> None:
        with self.lock:
            if self.started or self.phase in {'preparing', 'opening_login', 'awaiting_login'}:
                raise ValueError('Finish this queue before adding files.')
            self.jobs.append(job)
            self.phase = 'ready' if self.all_prepared() else 'idle'
        self.notify()

    def select(self, job_id: str, row_number: int | None, selected: bool) -> None:
        with self.lock:
            if self.started or self.phase in {'preparing', 'opening_login', 'awaiting_login'}:
                return
            job = next(j for j in self.jobs if j.id == job_id)
            for row in job.rows:
                if (row_number is None or row.row.row_number == row_number) and row.row.enabled and row.count:
                    row.selected = selected
            self.phase = 'ready' if self.all_prepared() else 'idle'
        self.notify()

    def all_prepared(self) -> bool:
        required = [j for j in self.jobs if j.selected_count]
        return bool(required) and all(j.prepared for j in required)

    def _session(self, job: Job):
        if job.id not in self.sessions:
            self.sessions[job.id] = self.session_factory(job, self)
        return self.sessions[job.id]

    def server_message(self, message: str) -> None:
        with self.lock:
            if message != self.message:
                self.message = message
                self.notify()

    def _first_unprepared_job(self):
        return next((j for j in self.jobs if j.selected_count and not j.prepared), None)

    def prepare(self) -> None:
        """Open login only. No Control Room click happens until explicit confirmation."""
        self.open_login()

    def open_login(self) -> None:
        with self.lock:
            if self.started:
                return
            job = self._first_unprepared_job()
            if job is None:
                self.phase = 'ready' if self.all_prepared() else 'idle'
                self.notify()
                return
            self.phase, self.current_id = 'opening_login', job.id
            self.message = 'Opening Edge. Log in, then click I’m logged in.'
            self.interrupt.clear()
        self.notify()
        try:
            row = next(r.row for r in job.rows if r.selected)
            self._session(job).open_login(row)
            with self.lock:
                self.phase = 'awaiting_login'
                self.message = 'Log in in Edge, then click I’m logged in.'
        except PreparationInterrupted:
            self.phase = 'idle'
            self.current_id = None
        except Exception as exc:
            self.log(f'Open login | {type(exc).__name__}: {exc}')
            self.phase = 'idle'
            self.current_id = None
            self.message = 'Could not open Edge. Click Open PD Board to try again.'
        self.notify()

    def confirm_login(self) -> None:
        with self.lock:
            if self.started or self.phase != 'awaiting_login':
                return
            job = next(j for j in self.jobs if j.id == self.current_id)
            self.phase, self.message = 'preparing', 'Minimizing Edge and preparing the board…'
            self.interrupt.clear()
        self.notify()
        try:
            row = next(r.row for r in job.rows if r.selected)
            self._session(job).confirm_login(row)
            job.prepared = True
        except PreparationInterrupted:
            self.phase, self.current_id = 'idle', None
            self.notify()
            return
        except Exception as exc:
            self.log(f'Confirm login | {type(exc).__name__}: {exc}')
            self.phase = 'awaiting_login'
            self.message = str(exc)
            self.notify()
            return
        if self._first_unprepared_job() is not None:
            self.phase, self.current_id, self.message = 'idle', None, 'Open the next board to log in.'
            self.notify()
        else:
            self.phase, self.current_id, self.message = 'ready', None, 'Ready'
            self.notify()

    def back(self) -> None:
        with self.condition:
            self.interrupt.set()
            if self.phase == 'awaiting_login':
                self.phase = 'ready' if self.all_prepared() else 'idle'
                self.current_id = None
            elif self.phase == 'reauth_login':
                self.phase = 'reauth'
            self.condition.notify_all()
        self.notify()

    def request_login_action(self) -> None:
        """GUI command; actual Edge work remains on its owning worker thread."""
        with self.condition:
            if self.phase == 'reauth':
                self._login_command = 'open'
                self.phase = 'reauth_opening'
            elif self.phase == 'reauth_login':
                self._login_command = 'confirm'
                self.phase = 'reauth_preparing'
            else:
                return
            self.interrupt.clear()
            self.condition.notify_all()
        self.notify()

    def request_pause(self) -> None:
        with self.condition:
            if self.phase in {'running', 'pausing'}:
                self.pause_requested = True
                self.interrupt.set()  # Only preparation observes this; never abort a submit.
                self.phase = 'pausing'
                self.message = 'Finishing the current operation before pausing.'
                self.condition.notify_all()
        self.notify()

    def resume(self) -> None:
        with self.condition:
            if self.phase != 'paused':
                return
            self.recovering = self.phase == 'reauth'
            self.pause_requested = False
            self.interrupt.clear()
            self.phase = 'running'
            self.message = 'Waiting for login in the managed Edge window.' if self.recovering else ''
            if not self.recovering:
                self._clock_on()
            self.condition.notify_all()
        self.notify()

    def request_skip(self) -> None:
        with self.condition:
            if self.phase not in {'paused', 'blocked', 'reauth', 'reauth_login'} or self.current_id is None:
                return
            self.skip_id = self.current_id
            self.pause_requested = False
            self.interrupt.set()
            self.condition.notify_all()

    def request_cancel(self) -> None:
        with self.condition:
            self.cancel_requested = True
            self.interrupt.set()
            self.condition.notify_all()

    def confirm_unknown_saved(self) -> None:
        with self.condition:
            if self.phase == 'blocked' and self.blocked_row is not None:
                self.blocked_row.set_status('Uploaded', 'User verified the saved images on the board.')
                self.log('User verified the current row on the board; marked uploaded once.')
                self.blocked_row = None
                self.phase = 'paused'
                self.pause_requested = True
                self.condition.notify_all()
        self.notify()

    def request_view(self, job_id):
        with self.condition:
            self.view_job_id = job_id
            self.condition.notify_all()

    def defer_unknown(self):
        with self.condition:
            if self.phase != 'blocked' or self.blocked_row is None:
                return
            self.blocked_row.reason = 'Deferred for review; not automatically resubmitted.'
            self.blocked_row = None
            self.phase = 'running'
            self.pause_requested = False
            self.interrupt.clear()
            self._clock_on()
            self.condition.notify_all()
        self.notify()

    def guard_preparation(self) -> None:
        if self.shutdown.is_set() or self.interrupt.is_set():
            raise PreparationInterrupted()

    def session_ready(self) -> None:
        with self.lock:
            if self.recovering:
                self.recovering = False
                if self.phase == 'running':
                    self._clock_on()
        self.notify()

    def validate_current_images(self) -> None:
        job = next(j for j in self.jobs if j.id == self.current_id)
        row = next(r for r in job.rows if r.row.row_number == self.current_row)
        row.preflight()

    def claim_submission(self) -> None:
        # Atomic boundary with Pause/Cancel commands from the GUI.
        with self.lock:
            if self.pause_requested or self.cancel_requested or self.shutdown.is_set():
                raise PreparationInterrupted()

    def _gate(self, job: Job) -> bool:
        login_states = {'reauth', 'reauth_opening', 'reauth_login', 'reauth_preparing'}
        while True:
            with self.lock:
                view_id, self.view_job_id = self.view_job_id, None
            if view_id in self.sessions:
                try:
                    self.sessions[view_id].view()
                except Exception as exc:
                    self.log(f'View Edge | {type(exc).__name__}')
            command = None
            with self.condition:
                if self.cancel_requested or self.shutdown.is_set():
                    return False
                if self.skip_id == job.id:
                    for row in job.rows:
                        if row.selected and row.status in {'Waiting', 'Paused'}:
                            row.set_status('Skipped', 'Board skipped by user.')
                    job.state = 'Skipped'
                    self.skip_id = None
                    self._login_command = None
                    self.interrupt.clear()
                    self.recovering = False
                    self.blocked_row = None
                    self.phase = 'running'
                    self._clock_on()
                    self.notify()
                    return False
                if self._login_command:
                    command, self._login_command = self._login_command, None
                elif not self.pause_requested and self.phase not in {'blocked'} | login_states:
                    return True
                else:
                    self._clock_off()
                    if self.phase not in {'blocked', 'paused'} | login_states:
                        self.phase, self.message = 'paused', 'Paused'
                        self.notify()
                    self.condition.wait(.2)
            session = self.sessions.get(job.id)
            if command:
                row = next(r.row for r in job.rows if r.row.row_number == self.current_row)
                try:
                    if command == 'open':
                        session.open_login(row)
                        self.phase, self.message = 'reauth_login', 'Log in in Edge, then click I’m logged in.'
                    else:
                        session.confirm_login(row)
                        job.prepared = True
                        self.recovering = False
                        self.pause_requested = False
                        self.phase, self.message = 'running', ''
                        self._clock_on()
                except PreparationInterrupted:
                    self.phase = 'reauth'
                except Exception as exc:
                    self.log(f'Reauthentication | {type(exc).__name__}: {exc}')
                    self.phase = 'reauth_login' if command == 'confirm' else 'reauth'
                    self.message = str(exc)
                self.notify()
            elif session is not None:
                session.pump()

    def run(self, *, retry: bool = False) -> None:
        with self.lock:
            if self._running:
                return
            if not retry and (self.started or not self.all_prepared()):
                raise ValueError('Prepare the required sessions before starting.')
            if retry and (not self.started or self.phase != 'finished'):
                return
            if retry:
                for job in self.jobs:
                    for row in job.rows:
                        if row.status == 'Failed' and row.safe_retry:
                            row.set_status('Waiting')
                            row.safe_retry = False
            else:
                self.total = sum(j.selected_count for j in self.jobs)
                self.started = True
            self.cancel_requested = self.pause_requested = False
            self.interrupt.clear()
            self.phase, self._running = 'running', True
            self._clock_on()
        self.notify()
        try:
            active_job = None
            for recovery_pass in range(2):
                if self.cancel_requested or self.shutdown.is_set():
                    break
                for job in self.jobs:
                    if recovery_pass:
                        for pending in job.rows:
                            if pending.status == 'Failed' and pending.safe_retry:
                                pending.set_status('Waiting')
                                pending.safe_retry = False
                    if not job.selected_count or not any(r.selected and r.status == 'Waiting' for r in job.rows):
                        continue
                    self.current_id = job.id
                    job.state = 'Uploading'
                    self.notify()
                    for row in job.rows:
                        if not row.selected or row.status != 'Waiting':
                            continue
                        self.current_row = row.row.row_number
                        while row.status == 'Waiting':
                            if not self._gate(job):
                                break
                            try:
                                upload_row = row.upload_row()
                                if any(row.row.image_paths) and not upload_row.image_count:
                                    raise ConfigError(row.reason or 'No readable images remain in this row.')
                                row.status = 'Uploading'
                                if not any(row.row.image_paths):
                                    row.set_status('Uploading')
                                self.notify()
                                session = self._session(job)
                                if active_job != job.id:
                                    if hasattr(session, 'activate'):
                                        session.activate(upload_row)
                                    active_job = job.id
                                session.upload(upload_row)
                                row.upload_complete()
                                self.log(f'job {job.id[:8]} row {row.row.row_number} | {row.status}')
                            except PreparationInterrupted:
                                row.set_status('Waiting')
                            except (ReauthenticationNeeded, TargetMismatch) as exc:
                                row.set_status('Waiting', str(exc))
                                active_job = None
                                job.prepared = False
                                session = self.sessions.get(job.id)
                                if session is not None:
                                    session.invalidate()
                                with self.lock:
                                    self._clock_off()
                                    self.phase = 'reauth'
                                    self.message = str(exc)
                            except (OutcomeUnknown, UploadRejected) as exc:
                                row.status, row.reason = 'Outcome unknown', str(exc)
                                for slot, status in row.images.items():
                                    if status == 'Uploading':
                                        row.images[slot] = 'Outcome unknown'
                                row.safe_retry = False
                                self.blocked_row = row
                                with self.lock:
                                    self._clock_off()
                                    self.phase = 'blocked'
                                    self.message = str(exc)
                                self.notify()
                                self._gate(job)
                            except ConfigError as exc:
                                row.set_status('Failed', str(exc))
                                row.safe_retry = True  # No form submission has occurred.
                            except Exception as exc:
                                # The adapter guarantees other exceptions occur before submit.
                                row.set_status('Failed', 'Preparation failed; no upload was submitted.')
                                row.safe_retry = True
                                self.log(f'job {job.id[:8]} row {row.row.row_number} | {type(exc).__name__}: {exc}')
                            self.notify()
                        if self.cancel_requested or self.shutdown.is_set():
                            break
                    statuses = {r.status for r in job.rows if r.selected}
                    job.state = 'Uploaded' if statuses == {'Uploaded'} else ('Skipped' if statuses == {'Skipped'} else 'Incomplete')
                    if self.cancel_requested or self.shutdown.is_set():
                        break
        finally:
            with self.lock:
                if self.cancel_requested or self.shutdown.is_set():
                    for job in self.jobs:
                        for row in job.rows:
                            if row.selected and row.status in {'Waiting', 'Paused'}:
                                row.set_status('Cancelled', 'Queue cancelled by user.')
                        if any(r.status == 'Cancelled' for r in job.rows):
                            job.state = 'Cancelled'
                    self.phase = 'finished' if self.total and self.snapshot()['done'] == self.total else 'cancelled'
                else:
                    self.phase = 'finished'
                self._clock_off()
                self._running = False
                self.current_id = self.current_row = None
                self.message = 'Completed' if self.snapshot()['done'] == self.total else 'Finished with exceptions'
            self.notify()

    def remove(self, job_id: str) -> None:
        # Worker thread only: closes only this job's managed resources.
        if self.started:
            return
        session = self.sessions.pop(job_id, None)
        if session:
            session.close()
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != job_id]
            self.phase = ('ready' if self.all_prepared() else 'idle') if self.jobs else 'empty'
        self.notify()

    def clear(self) -> None:
        self.close_sessions()
        with self.lock:
            self.jobs.clear()
            self.phase, self.started = 'empty', False
            self.current_id = self.current_row = None
            self.message = ''
            self.total = 0
            self._elapsed, self._clock = 0, None
            self.blocked_row = None
            self.recovering = False
        self.notify()

    def close_sessions(self) -> None:
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()
