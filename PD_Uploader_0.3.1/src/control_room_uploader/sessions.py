"""Production adapter over the existing form automation, with explicit ownership."""
from __future__ import annotations
import re
import tempfile
from urllib.parse import urlsplit, parse_qs
from .bot import ControlRoomBot, BotUserError, SiteImagesTransientError, _matching_option
from .flow import PreparationInterrupted, ReauthenticationNeeded, TargetMismatch, OutcomeUnknown, UploadRejected


def target_parts(url: str) -> tuple[str, str | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise TargetMismatch('The CSV needs a valid http(s) page_url without embedded credentials.')
    origin = f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'
    query = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    site = query.get('siteid', [None])[0]
    return origin, site


def classify_popup(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', text).strip().casefold()
    if re.search(r'\b(error|failed|failure|invalid|denied|unable|cannot|could not|not saved|not uploaded|unsuccessful|not successful)\b', normalized):
        return 'error'
    if (re.search(r'\b(upload(?:ed)?|sav(?:e|ed))\b', normalized)
            and re.search(r'\b(success|successful|successfully)\b', normalized)):
        return 'success'
    if re.fullmatch(r'(?:all |the )?images? (?:has |have |was |were )?(?:been )?uploaded[.!]?', normalized):
        return 'success'
    return 'unknown'

class SessionPool:
    """Created on GUI thread, but started/used/stopped only by the permanent worker."""
    def __init__(self):
        self.runtime = None

    def factory(self, job, engine):
        return ManagedSession(job.id, engine, self)

    def get_runtime(self):
        if self.runtime is None:
            from playwright.sync_api import sync_playwright
            self.runtime = sync_playwright().start()
        return self.runtime

    def close(self):
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

class ManagedSession(ControlRoomBot):
    def __init__(self, job_id, engine, pool):
        self.temporary = tempfile.TemporaryDirectory(prefix='pd-uploader-031-')
        from pathlib import Path
        super().__init__(Path(self.temporary.name), lambda m: engine.log(f'job {job_id[:8]} | {m}'))
        self.engine, self.pool = engine, pool
        self.job_id = job_id
        self.page = None
        self.target = None
        self.expected_site = None
        self.submitted = False
        self.awaiting_dialog = False
        self.feedback: list[str] = []
        self._stop_event = engine.interrupt
        self.login_approved = False
        self.opened_url = None

    def _ensure_context(self):
        if self._context is not None:
            try:
                browser = self._context.browser
                if browser is not None and browser.is_connected():
                    return self._context
            except Exception:
                pass
            self._context_closed()
        try:
            runtime = self.pool.get_runtime()
            self._context = runtime.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), channel='msedge', headless=False,
                no_viewport=True, chromium_sandbox=True, accept_downloads=False,
            )
            self._context.set_default_timeout(15_000)
            self._context.expose_function('__uploaderShouldStop',
                lambda: not self.submitted and self.engine.interrupt.is_set())
            self._context.on('close', self._context_closed)
            self._install_dialog_handler()
            return self._context
        except Exception as exc:
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
                self._context_closed()
            raise BotUserError('Could not open managed Microsoft Edge. Check that Edge is installed and permitted by IT.') from exc

    def _context_closed(self, *_args):
        super()._context_closed()
        self.page = None
        self.target = None
        self.login_approved = False
        self.opened_url = None

    def _first_page(self):
        context = self._ensure_context()
        if self.page is None or self.page.is_closed():
            self.page = context.pages[0] if context.pages else context.new_page()
            self._install_page_dialog_handler(self.page)
        return self.page

    def _current_page_for_run(self):
        if self._context is None or self.page is None or self.page.is_closed():
            raise ReauthenticationNeeded('The managed browser was closed. Resume to reopen it and log in.')
        return self.page

    def _install_page_dialog_handler(self, page):
        if id(page) in self._dialog_handler_page_ids:
            return
        def accept(dialog):
            text = dialog.message
            try:
                dialog.accept()
            except Exception:
                if self.awaiting_dialog:
                    self.feedback.append('unknown')
                return
            if self.awaiting_dialog:
                # Preserve the original uploader's signal: an accepted post-Upload popup.
                # Its wording is not a new prerequisite for continuing the queue.
                self.feedback.append('success')
            # Never put arbitrary popup contents or secrets into diagnostic logs.
            self.log('Browser popup handled; ' + ('upload feedback recorded.' if self.awaiting_dialog else 'outside upload.'))
        page.on('dialog', accept)
        self._dialog_handler_page_ids.add(id(page))

    def _check_stopped(self):
        if not self.submitted:
            self.engine.guard_preparation()

    def _check_access_denied(self, page):
        body = self._body_text(page, timeout=1_000).casefold()
        if re.search(r'access denied|not authori[sz]ed|you have been logged out|session (?:has )?expired', body):
            raise ReauthenticationNeeded('Login is required in the assigned Edge window. Resume after checking login.')

    def _wait_for_server(self, page, action, description):
        self.engine.server_message('Waiting for server: ' + description)
        while True:
            self._check_stopped()
            if page.is_closed():
                raise ReauthenticationNeeded('The managed browser was closed. Resume to reopen it.')
            if self.engine.started and not self.engine.recovering:
                self._check_access_denied(page)
            try:
                return action()
            except (PreparationInterrupted, ReauthenticationNeeded, TargetMismatch):
                raise
            except Exception as exc:
                # Only Playwright timeouts and known partial-form states are transient waits.
                from playwright.sync_api import TimeoutError as PlaywrightTimeout
                if not isinstance(exc, (PlaywrightTimeout, SiteImagesTransientError)):
                    raise
                # Login may be in progress; keep waiting before any submit.
                page.wait_for_timeout(200)

    def _ensure_control_room_landing(self, page):
        # Preserve the existing navigation, while allowing Pause/Back to propagate intact.
        if self._page_has_file_inputs(page) or self._addimages_exists(page):
            return
        button = page.locator('#controlroom-button').first
        self._wait_for_server(page, lambda: button.wait_for(state='visible', timeout=1_000),
                              'login / Control Room button')
        self._check_stopped()
        button.click(timeout=10_000)
        self._wait_for_server(page, lambda: self._wait_for_control_room_landing(page),
                              'Control Room page')

    def _wait_for_select_option(self, page, field, value, field_name, row_number):
        self.engine.server_message(f'Waiting for server: {field_name} options. Pause is available.')
        while True:
            self._check_stopped()
            if page.is_closed():
                raise ReauthenticationNeeded('The managed browser closed while loading options.')
            self._check_access_denied(page)
            option = _matching_option(self._select_options(field), value)
            if option is not None:
                return option
            page.wait_for_timeout(100)

    def _navigate(self, page, url):
        self.engine.guard_preparation()
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=1_000)
        except Exception as exc:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            if not isinstance(exc, PlaywrightTimeout):
                raise BotUserError('Could not open the destination. Check connectivity and try again.') from exc
        # Do not reload a pending navigation when the server is slow.

    def open_login(self, row):
        self.submitted = False
        self.login_approved = False
        self.engine.guard_preparation()
        target_parts(row.page_url)
        page = self._first_page()
        self.restore_owned_window(page)
        self._navigate(page, row.page_url)
        self.opened_url = row.page_url
        # Deliberately do not find/click Control Room or open any modal here.

    def _window_session(self, page):
        if self._context is None or page is not self.page:
            raise BotUserError('The assigned Edge window is not available. Open PD Board again.')
        client = self._context.new_cdp_session(page)
        try:
            window = client.send('Browser.getWindowForTarget')
            return client, window['windowId']
        except Exception:
            client.detach()
            raise

    def minimize_owned_window(self, page):
        """Only the window belonging to this managed page, never an arbitrary Edge."""
        self.engine.guard_preparation()
        client = None
        try:
            client, window_id = self._window_session(page)
            bounds = client.send('Browser.getWindowBounds', {'windowId': window_id})['bounds']
            if bounds.get('windowState') == 'fullscreen':
                client.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'normal'}})
            client.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'minimized'}})
            for _ in range(20):
                self.engine.guard_preparation()
                actual = client.send('Browser.getWindowBounds', {'windowId': window_id})['bounds']
                if actual.get('windowState') == 'minimized':
                    return
                page.wait_for_timeout(100)
            raise BotUserError('Edge did not minimize. No Control Room navigation was started. Try again.')
        except PreparationInterrupted:
            raise
        except Exception as exc:
            raise BotUserError('Could not minimize the assigned Edge window. No background preparation was started. Open the board again and retry.') from exc
        finally:
            if client is not None:
                client.detach()

    def restore_owned_window(self, page):
        client = None
        try:
            client, window_id = self._window_session(page)
            client.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': 'normal'}})
        except Exception:
            # Restoring for an explicit login is allowed even if window-state control fails.
            pass
        finally:
            if client is not None:
                client.detach()
        page.bring_to_front()

    def confirm_login(self, row):
        page = self._current_page_for_run()
        self.engine.guard_preparation()
        self.minimize_owned_window(page)
        self.login_approved = True
        # This is the first point at which Control Room preparation is permitted.
        return self.prepare(row)

    def prepare(self, row):
        self.submitted = False
        self.engine.guard_preparation()
        if not self.login_approved:
            raise ReauthenticationNeeded('Open PD Board and confirm I’m logged in before continuing.')
        page = self._current_page_for_run()
        origin, site = target_parts(row.page_url)
        self.expected_site = site
        self.minimize_owned_window(page)
        if self.opened_url != row.page_url:
            self._navigate(page, row.page_url)
            self.opened_url = row.page_url
        self._ensure_control_room_landing(page)
        panel = self._open_site_images_panel(page)
        if self.expected_site is None:
            # For URLs without a site key, bind evidence only on the explicitly requested
            # document, never a guessed filename or unrelated redirected destination.
            current = urlsplit(page.url)
            requested = urlsplit(row.page_url)
            if (current.scheme, current.netloc, current.path, current.query) != (requested.scheme, requested.netloc, requested.path, requested.query):
                raise TargetMismatch('Could not verify this CSV destination after navigation. Check its page_url.')
            self.expected_site = self._site_value(page)
            if not self.expected_site:
                raise TargetMismatch('The page has no verifiable destination identifier. No images were submitted.')
        self._verify_target(page, origin)
        self.target = row.page_url
        return panel

    def _site_value(self, page):
        return str(page.evaluate("() => document.querySelector('#crsite')?.value || ''"))

    def _verify_target(self, page, expected_origin):
        actual_origin, _ = target_parts(page.url)
        actual_site = self._site_value(page)
        if actual_origin != expected_origin or not actual_site or actual_site != self.expected_site:
            raise TargetMismatch('The page does not match the CSV destination. No images were submitted; check the managed Edge window.')

    def upload(self, row):
        self.submitted = False
        try:
            page = self._current_page_for_run()
            if not self.login_approved:
                raise ReauthenticationNeeded("Open PD Board and confirm I’m logged in before continuing.")
            self.minimize_owned_window(page)
            if self.target != row.page_url:
                panel = self.prepare(row)
            else:
                self._check_access_denied(page)
                # A login redirect needs explicit recovery rather than waiting for form controls.
                if target_parts(page.url)[0] != target_parts(row.page_url)[0]:
                    raise ReauthenticationNeeded('The page redirected away from the board. Resume to check login.')
                self._verify_target(page, target_parts(row.page_url)[0])
                panel = self._open_site_images_panel(page)
            self.engine.session_ready()
            for attempt in range(2):
                try:
                    self._set_row_fields(panel, page, row)
                    break
                except SiteImagesTransientError:
                    if attempt:
                        raise
                    panel = self._reset_site_images_panel(page, row.row_number)
            self.engine.validate_current_images()
            self._assign_images(panel, row)
            self.engine.validate_current_images()
            self.engine.guard_preparation()
            self._verify_target(page, target_parts(row.page_url)[0])
            self.engine.claim_submission()
            self.feedback.clear()
            self.awaiting_dialog = True
            # From here any exception is an unknown outcome. Never auto-replay it.
            self.submitted = True
            self._click_upload(panel, page, row.row_number)
        except (PreparationInterrupted, ReauthenticationNeeded, TargetMismatch):
            if self.submitted:
                raise OutcomeUnknown('Connection or session changed after Upload. Check the saved images before continuing.')
            raise
        except (OutcomeUnknown, UploadRejected):
            raise
        except Exception as exc:
            if self.submitted:
                raise OutcomeUnknown('Upload outcome is unknown. Check the saved images before continuing.') from exc
            raise
        finally:
            self.awaiting_dialog = False
            self.submitted = False

    def _wait_for_upload_feedback(self, page, dialog_count_before):
        self.engine.server_message('Waiting for server confirmation. The next row will not start yet.')
        while True:
            if self.feedback:
                result = self.feedback.pop(0)
                if result == 'success':
                    return
                if result == 'error':
                    raise UploadRejected('The server reported an upload error. Check whether any images were saved before retrying.')
                raise OutcomeUnknown('The upload popup was not a recognized success message. Check the saved images.')
            if self.engine.shutdown.is_set():
                raise OutcomeUnknown('The application closed before upload confirmation. The server may still save the images.')
            if page.is_closed():
                raise OutcomeUnknown('Edge closed before confirmation. Check whether the images were saved.')
            # Pause/Cancel do not cancel this in-flight server write.
            page.wait_for_timeout(200)

    def _log_current_page(self, page):
        pass

    def _save_debug_output(self, page, reason):
        self.log('Debug capture omitted to protect page data; operation: ' + reason)

    def invalidate(self):
        self.target = None
        self.login_approved = False

    def pump(self):
        try:
            if self.page is not None and not self.page.is_closed():
                self.page.wait_for_timeout(10)
        except Exception:
            pass

    def close(self):
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = self.page = None
        self._dialog_handler_installed = False
        self._dialog_handler_page_ids.clear()
        try:
            self.temporary.cleanup()
        except OSError:
            # An OS/Edge file lock can outlive context closure; never kill unrelated processes.
            self.log('Temporary profile cleanup was delayed by an operating-system file lock.')
