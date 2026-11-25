from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import UploadRow
from .paths import logs_dir


LogFn = Callable[[str], None]
CONTROL_ROOM_BUTTON_TIMEOUT_MS = 180_000
LANDING_TIMEOUT_MS = 30_000
SITE_IMAGES_TIMEOUT_MS = 30_000
SITE_IMAGES_TRIGGER_TIMEOUT_MS = 1_000
SITE_IMAGES_CONTROLS_READY_TIMEOUT_MS = 2_000
SITE_IMAGES_CONTROLS_STABLE_MS = 400
SITE_IMAGES_ROW_RETRY_ATTEMPTS = 2
DEFAULT_SELECT_OPTION_TIMEOUT_MS = 25_000
DEPARTMENT_SELECT_OPTION_TIMEOUT_MS = 25_000
SELECT_OPTION_POLL_MS = 100
SELECT_ACTION_TIMEOUT_MS = 3_000
UPLOAD_DIALOG_TIMEOUT_MS = 15_000
UPLOAD_NO_DIALOG_SETTLE_MS = 3_000


class BotUserError(Exception):
    """A clear, user-actionable bot error."""


class BotStopped(BotUserError):
    """The user cancelled an operation before upload."""


class SelectOptionUnavailableError(BotUserError):
    """Raised when a dependent select never receives the requested option."""


class SiteImagesTransientError(BotUserError):
    """Raised when the Site Images form is temporarily half-loaded."""


class ControlRoomBot:
    def __init__(self, profile_dir: Path, log: LogFn | None = None) -> None:
        self.profile_dir = profile_dir
        self.log = log or (lambda message: None)
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._dialog_handler_installed = False
        self._dialog_handler_page_ids: set[int] = set()
        self._dialog_count = 0
        self._stop_event: threading.Event | None = None
        self._site_images_needs_reset = False

    def open_browser(self, start_url: str | None = None) -> None:
        context = self._ensure_context()
        page = self._first_page()
        page.bring_to_front()
        if start_url:
            self.log(f"Opening browser profile at: {start_url}")
            page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        else:
            self.log("Opening browser profile. Use this browser window for login.")

    def run_rows(
        self,
        rows: list[UploadRow],
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._stop_event = stop_event
        enabled_rows = [row for row in rows if row.enabled]
        if not enabled_rows:
            raise BotUserError("No enabled rows found in the CSV.")

        self._current_page_for_run()

        run_started_at = datetime.now()
        run_started_monotonic = time.monotonic()
        run_status = "running"
        self.log(f"Run started at: {run_started_at:%Y-%m-%d %H:%M:%S}")
        self.log(f"Starting upload run for {len(enabled_rows)} enabled row(s).")

        try:
            for index, row in enumerate(enabled_rows, start=1):
                if stop_event and stop_event.is_set():
                    run_status = "stopped"
                    self.log("Run stopped by user.")
                    return

                row_started_monotonic = time.monotonic()
                page = self._current_page_for_run()
                self.log(f"Row {row.row_number} ({index}/{len(enabled_rows)}): using current browser page.")
                page.bring_to_front()
                self._log_current_page(page)

                self._check_access_denied(page)
                self._ensure_control_room_landing(page)
                if self._site_images_needs_reset:
                    self.log(
                        f"Row {row.row_number}: previous Site Images form did not stabilise; "
                        "resetting modal before this row."
                    )
                    panel = self._reset_site_images_panel(page, row.row_number)
                    self._site_images_needs_reset = False
                else:
                    panel = self._open_site_images_panel(page)

                for attempt in range(1, SITE_IMAGES_ROW_RETRY_ATTEMPTS + 1):
                    try:
                        self._set_row_fields(panel, page, row)
                        self._assign_images(panel, row)
                        break
                    except SiteImagesTransientError as exc:
                        if attempt >= SITE_IMAGES_ROW_RETRY_ATTEMPTS:
                            raise BotUserError(
                                f"Row {row.row_number}: Site Images form stayed half-loaded after retry. {exc}"
                            ) from exc
                        self.log(
                            f"Row {row.row_number}: Site Images form was half-loaded ({exc}); "
                            "resetting modal and retrying this row."
                        )
                        panel = self._reset_site_images_panel(page, row.row_number)

                self._check_stopped()
                self._click_upload(panel, page, row.row_number)
                self.log(f"Row {row.row_number}: upload click completed.")
                self._wait_for_site_images_after_upload(page)
                row_elapsed = time.monotonic() - row_started_monotonic
                self.log(f"Row {row.row_number}: completed in {_format_duration(row_elapsed)}.")
            run_status = "finished"
        except BotStopped:
            run_status = "stopped"
            self._site_images_needs_reset = True
        except Exception:
            self._site_images_needs_reset = True
            run_status = "failed"
            raise
        finally:
            run_elapsed = time.monotonic() - run_started_monotonic
            run_finished_at = datetime.now()
            if run_status == "finished":
                self.log(f"Run finished at: {run_finished_at:%Y-%m-%d %H:%M:%S}")
                self.log(f"Total upload time: {_format_duration(run_elapsed)}.")
                self.log("Run finished.")
            elif run_status == "stopped":
                self.log(f"Run stopped at: {run_finished_at:%Y-%m-%d %H:%M:%S}")
                self.log(f"Total upload time before stop: {_format_duration(run_elapsed)}.")
            else:
                self.log(f"Run failed at: {run_finished_at:%Y-%m-%d %H:%M:%S}")
                self.log(f"Total upload time before error: {_format_duration(run_elapsed)}.")

    def close(self) -> None:
        self._dialog_handler_installed = False
        self._dialog_handler_page_ids.clear()
        self._dialog_count = 0
        self._site_images_needs_reset = False
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BotUserError(
                "Playwright is not installed. Install requirements or use the packaged Windows .exe."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="msedge",
                headless=False,
                accept_downloads=True,
                no_viewport=True,
                chromium_sandbox=True,
                args=["--start-maximized"],
            )
        except PlaywrightError as exc:
            self._playwright.stop()
            self._playwright = None
            raise BotUserError(
                "Could not open Microsoft Edge through the app. Make sure Microsoft Edge is installed."
            ) from exc

        self._context.expose_function("__uploaderShouldStop",
                                      lambda: bool(self._stop_event and self._stop_event.is_set()))
        self._context.on("close", self._context_closed)
        self._context.set_default_timeout(15_000)
        self._install_dialog_handler()
        self.log(f"Browser profile: {self.profile_dir}")
        return self._context

    def _context_closed(self, *_args: Any) -> None:
        self._context = None
        self._dialog_handler_installed = False
        self._dialog_handler_page_ids.clear()

    def _check_stopped(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise BotStopped("Stopped by user.")

    def _wait_for_server(self, page: Any, action: Callable[[], Any], description: str) -> Any:
        """Keep the existing request alive; never re-click a trigger while waiting."""
        self.log(f"Waiting for server: {description}. Stop is available.")
        while True:
            self._check_stopped()
            if page.is_closed():
                raise BotUserError("Browser was closed. Click Open Browser / Login to reconnect.")
            try:
                return action()
            except BotStopped:
                raise
            except Exception:
                self._check_access_denied(page)
                page.wait_for_timeout(200)

    def _first_page(self) -> Any:
        context = self._ensure_context()
        pages = [page for page in context.pages if not page.is_closed()]
        if pages:
            page = pages[0]
        else:
            page = context.new_page()
        self._install_page_dialog_handler(page)
        return page

    def _current_page_for_run(self) -> Any:
        if self._context is None:
            raise BotUserError(
                "Browser is not open. Click Open Browser / Login, log in manually, then click Run."
            )

        pages = [page for page in self._context.pages if not page.is_closed()]
        if not pages:
            raise BotUserError(
                "No browser page is open. Click Open Browser / Login, log in manually, then click Run."
            )
        page = pages[0]
        self._install_page_dialog_handler(page)
        return page

    def _log_current_page(self, page: Any) -> None:
        url, title = self._page_url_and_title(page)
        self.log(f"Current page URL: {url}")
        self.log(f"Current page title: {title}")

    def _ensure_control_room_landing(self, page: Any) -> None:
        if self._page_has_file_inputs(page):
            self.log("Site Images modal is already open.")
            return

        if self._addimages_exists(page):
            self.log("Already on Control Room Landing Page; Site Images trigger is present.")
            return

        self.log("Waiting for manual login / Control Room button (#controlroom-button).")
        control_room_button = page.locator("#controlroom-button").first
        try:
            self._wait_for_server(
                page, lambda: control_room_button.wait_for(state="visible", timeout=1_000),
                "login / Control Room button",
            )
        except BotStopped:
            raise
        except Exception as exc:
            diagnostics = self._control_room_button_diagnostics(page)
            self.log("Control Room button diagnostics: " + diagnostics)
            raise BotUserError(
                "Control Room button (#controlroom-button) did not become visible after login. "
                + diagnostics
            ) from exc

        self.log("Clicking Control Room (#controlroom-button).")
        control_room_button.click(timeout=10_000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass

        self.log("Waiting for Control Room Landing Page.")
        try:
            self._wait_for_server(page, lambda: self._wait_for_control_room_landing(page),
                                  "Control Room Landing Page")
        except BotStopped:
            raise
        except Exception as exc:
            self._save_debug_output(page, "control-room-landing-not-found")
            raise BotUserError(
                "Control Room Landing Page did not appear after clicking #controlroom-button."
            ) from exc

    def _wait_for_control_room_landing(self, page: Any) -> None:
        page.wait_for_function(
            """
            () => {
                const bodyText = document.body ? document.body.innerText : "";
                if (/Control Room Landing Page/i.test(bodyText)) {
                    return true;
                }
                if (document.querySelector("#view-image-modal")) {
                    return true;
                }
                return Array.from(document.querySelectorAll(".addimages")).some((element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none"
                        && style.visibility !== "hidden"
                        && rect.width > 0
                        && rect.height > 0;
                });
            }
            """,
            timeout=1_000,
        )

    def _addimages_exists(self, page: Any) -> bool:
        try:
            page.locator(".addimages").first.wait_for(state="attached", timeout=800)
            return True
        except Exception:
            return False

    def _page_has_file_inputs(self, page: Any) -> bool:
        try:
            page.locator("input[type=file]").first.wait_for(state="attached", timeout=500)
            return True
        except Exception:
            return False

    def _control_room_button_diagnostics(self, page: Any) -> str:
        unlock_state = self._selector_visibility_state(page, "#unlock-button")
        span_state = self._selector_visibility_state(page, "#controlroomSpan")
        body_text = self._body_text(page, timeout=1_500).lower()
        unlock_visible = "yes" if unlock_state == "visible" else "no"
        span_hidden = "yes" if span_state == "hidden" else "no"
        logged_out = "yes" if "you have been logged out" in body_text else "no"
        return (
            f"#unlock-button visible: {unlock_visible} ({unlock_state}); "
            f"#controlroomSpan hidden: {span_hidden} ({span_state}); "
            f"page says 'You have been logged out': {logged_out}"
        )

    def _selector_visibility_state(self, page: Any, selector: str) -> str:
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                return "not found"
            first = locator.first
            try:
                first.wait_for(state="visible", timeout=500)
                return "visible"
            except Exception:
                first.wait_for(state="hidden", timeout=500)
                return "hidden"
        except Exception:
            return "unknown"

    def _page_url_and_title(self, page: Any) -> tuple[str, str]:
        try:
            url = page.url
        except Exception:
            url = "<unavailable>"
        try:
            title = page.title() or "<blank>"
        except Exception:
            title = "<unavailable>"
        return url, title

    def _body_text(self, page: Any, *, timeout: int) -> str:
        try:
            return page.locator("body").inner_text(timeout=timeout)
        except Exception:
            return ""

    def _install_dialog_handler(self) -> None:
        if self._context is None or self._dialog_handler_installed:
            return

        for page in self._context.pages:
            self._install_page_dialog_handler(page)

        def handle_new_page(page: Any) -> None:
            self._install_page_dialog_handler(page)

        self._context.on("page", handle_new_page)
        self._dialog_handler_installed = True

    def _install_page_dialog_handler(self, page: Any) -> None:
        page_id = id(page)
        if page_id in self._dialog_handler_page_ids:
            return

        def accept_dialog(dialog: Any) -> None:
            try:
                self.log(f"Browser popup accepted automatically: {dialog.message}")
                dialog.accept()
                self._dialog_count += 1
            except Exception:
                pass

        try:
            page.on("dialog", accept_dialog)
            self._dialog_handler_page_ids.add(page_id)
        except Exception:
            pass

    def _check_access_denied(self, page: Any) -> None:
        body_text = self._body_text(page, timeout=4_000)

        lowered = body_text.lower()
        if "access denied" in lowered or "not authorized" in lowered or "not authorised" in lowered:
            raise BotUserError(
                "Access Denied. Log in using the browser opened by this app, then click Run again."
            )

    def _open_site_images_panel(self, page: Any) -> Any:
        if self._page_has_file_inputs(page):
            self.log("Site Images modal is already open.")
            return self._wait_for_site_images_modal(page, wait_for_controls_ready=True)

        self.log("Waiting for Site Images (.addimages).")
        click_targets: list[tuple[str, Callable[[], Any]]] = [
            (
                '.addimages[title="Add images for this site"]',
                lambda: page.locator('.addimages[title="Add images for this site"]').first,
            ),
            (".addimages", lambda: page.locator(".addimages").first),
            (
                'text "Site Images"',
                lambda: page.get_by_text(re.compile(r"^\s*site\s*images\s*$", re.I)).first,
            ),
        ]

        def find_trigger() -> tuple[str, Any]:
            for name, target_factory in click_targets:
                try:
                    target = target_factory()
                    target.wait_for(state="visible", timeout=200)
                    return name, target
                except Exception:
                    continue
            raise SiteImagesTransientError("Site Images trigger is still loading.")

        name, target = self._wait_for_server(page, find_trigger, "Site Images trigger")
        self._check_stopped()
        self.log(f"Opening Site Images modal using {name}.")
        target.click(timeout=3_000)
        panel = self._wait_for_site_images_modal(page, wait_for_controls_ready=True)
        self.log("Site Images modal opened.")
        return panel

    def _wait_for_site_images_modal(self, page: Any, *, wait_for_controls_ready: bool = False) -> Any:
        file_inputs = page.locator("input[type=file]")
        self._wait_for_server(
            page, lambda: file_inputs.first.wait_for(state="attached", timeout=1_000),
            "Site Images form",
        )

        file_input_count = file_inputs.count()
        self.log(f"Detected {file_input_count} input[type=file] control(s) in Site Images modal.")
        if file_input_count == 0:
            self._save_debug_output(page, "site-images-file-inputs-zero")
            raise BotUserError("Site Images modal opened, but no input[type=file] controls were found.")

        root = self._site_images_form_root(page)
        self._patch_site_images_change_func(page)
        if wait_for_controls_ready:
            def ready() -> None:
                if not self._wait_for_site_images_controls_ready(root, page):
                    raise SiteImagesTransientError("Site Images selectors did not initialise.")
            self._wait_for_server(page, ready, "Site Images selectors")
        return root

    def _patch_site_images_change_func(self, page: Any) -> None:
        try:
            result = page.evaluate(
                """
                () => {
                    const current = window.changeFunc;
                    if (typeof current !== "function") {
                        return "missing";
                    }
                    if (current.__controlRoomUploaderPatched === true) {
                        return "already";
                    }

                    const patched = function (obj, item) {
                        if (!obj || obj.selectedIndex === -1) {
                            return;
                        }

                        const selectedOption = obj.options ? obj.options[obj.selectedIndex] : null;
                        const selectedId = selectedOption ? selectedOption.id : "";
                        const hiddenFieldId = item === "shift" ? "shiftlist" : item;
                        const hiddenField = document.getElementById(hiddenFieldId);

                        if (hiddenField) {
                            hiddenField.value = selectedId;
                        } else {
                            console.warn(
                                "ControlRoomImageUploader: hidden field not found for",
                                item,
                                "mapped to",
                                hiddenFieldId
                            );
                        }

                        if (typeof window.GetIndexData === "function") {
                            window.GetIndexData(selectedId, item);
                        }
                    };

                    patched.__controlRoomUploaderPatched = true;
                    patched.__controlRoomUploaderOriginal = current;
                    window.changeFunc = patched;
                    return "patched";
                }
                """
            )
        except Exception:
            return

        if result == "patched":
            self.log("Applied Site Images changeFunc patch for shift -> shiftlist.")

    def _site_images_form_root(self, page: Any) -> Any:
        for selector in ("#view-image-modal .modal-body", "#view-image-modal form", "#view-image-modal"):
            try:
                candidate = page.locator(selector).first
                candidate.wait_for(state="attached", timeout=200)
                if candidate.locator("input[type=file]").count() > 0:
                    return candidate
            except Exception:
                continue
        return page

    def _wait_for_site_images_controls_ready(self, root: Any, page: Any) -> bool:
        self.log("Waiting for Site Images selectors to initialise.")
        elapsed_ms = 0
        stable_ms = 0
        last_state: tuple[tuple[str, str], ...] | None = None

        while elapsed_ms <= SITE_IMAGES_CONTROLS_READY_TIMEOUT_MS:
            self._check_stopped()
            state = _site_images_select_state(root)
            if _site_images_selects_ready(state):
                if state == last_state:
                    stable_ms += SELECT_OPTION_POLL_MS
                else:
                    last_state = state
                    stable_ms = 0
                if stable_ms >= SITE_IMAGES_CONTROLS_STABLE_MS:
                    return True
            else:
                last_state = state
                stable_ms = 0

            page.wait_for_timeout(SELECT_OPTION_POLL_MS)
            elapsed_ms += SELECT_OPTION_POLL_MS

        self.log("Site Images selectors did not fully stabilise before timeout.")
        return False

    def _wait_for_site_images_after_upload(self, page: Any) -> None:
        try:
            root = self._site_images_form_root(page)
            self._patch_site_images_change_func(page)
            if not self._wait_for_site_images_controls_ready(root, page):
                self._site_images_needs_reset = True
        except Exception:
            self._site_images_needs_reset = True

    def _reset_site_images_panel(self, page: Any, row_number: int) -> Any:
        self.log(f"Row {row_number}: resetting Site Images modal.")
        try:
            page.evaluate(
                """
                () => {
                    const modal = document.querySelector("#view-image-modal");
                    if (!modal) {
                        return;
                    }
                    const modalBody = modal.querySelector(".modal-body");
                    if (modalBody) {
                        modalBody.innerHTML = "";
                    }
                    if (window.jQuery) {
                        try {
                            window.jQuery("#view-image-modal").modal("hide");
                        } catch (_error) {
                            // Bootstrap is not always available in the same state after AJAX failures.
                        }
                    }
                    modal.classList.remove("show");
                    modal.style.display = "none";
                    modal.setAttribute("aria-hidden", "true");
                    document.body.classList.remove("modal-open");
                    document.querySelectorAll(".modal-backdrop").forEach((element) => element.remove());
                }
                """
            )
        except Exception:
            pass
        self._site_images_needs_reset = False
        return self._open_site_images_panel(page)

    def _set_row_fields(self, panel: Any, page: Any, row: UploadRow) -> None:
        if self._set_site_images_row_fields_direct(page, row):
            return

        self._set_named_field(panel, page, ("Tier",), row.tier, row.row_number)
        self._set_named_field(panel, page, ("Department",), row.department, row.row_number)
        try:
            self._set_named_field(panel, page, ("Shift",), row.shift, row.row_number)
        except SelectOptionUnavailableError:
            if not row.department:
                raise
            self.log(
                f"Row {row.row_number}: Shift options did not refresh after Department; "
                "requesting fresh Shift/Metric options and retrying Shift once."
            )
            if not self._refresh_department_dependents(page):
                raise
            page.wait_for_timeout(500)
            self._patch_site_images_change_func(page)
            self._set_named_field(panel, page, ("Shift",), row.shift, row.row_number)
        self._set_named_field(panel, page, ("Metrics", "Metric"), row.metric, row.row_number)

    def _set_site_images_row_fields_direct(self, page: Any, row: UploadRow) -> bool:
        if not all((row.tier, row.department, row.shift, row.metric)):
            return False

        payload = {
            "tier": row.tier,
            "department": row.department,
            "shift": row.shift,
            "metric": row.metric,
        }
        try:
            result = page.evaluate(
                """
                async ({ tier, department, shift, metric }) => {
                    const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                    const normalise = (value) => clean(value).toLocaleLowerCase();
                    const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

                    const optionData = (select) => Array.from(select?.options || []).map((option, index) => ({
                        index,
                        id: option.id || "",
                        value: option.value || "",
                        label: clean(option.label || ""),
                        text: clean(option.textContent || ""),
                        disabled: option.disabled === true,
                    }));

                    const formatOptions = (select) => {
                        const labels = optionData(select)
                            .filter((option) => !option.disabled)
                            .map((option) => option.label || option.text || option.value || option.id)
                            .filter(Boolean);
                        return labels.length ? labels.join(", ") : "<none>";
                    };

                    const findOption = (select, wanted) => {
                        const expected = normalise(wanted);
                        return optionData(select)
                            .filter((option) => !option.disabled)
                            .find((option) => {
                                return [option.label, option.text, option.value, option.id]
                                    .some((value) => normalise(value) === expected);
                            }) || null;
                    };

                    const selectedOption = (select) => {
                        if (!select || select.selectedIndex < 0) {
                            return null;
                        }
                        return select.options[select.selectedIndex] || null;
                    };

                    const setHidden = (id, option) => {
                        const hidden = document.getElementById(id);
                        if (hidden && option) {
                            hidden.value = option.id || option.value || clean(option.textContent || "");
                        }
                    };

                    const setSelect = (select, option, hiddenId) => {
                        select.selectedIndex = option.index;
                        setHidden(hiddenId, select.options[option.index]);
                    };

                    const getOnce = (url, params, targetSelector) => new Promise((resolve) => {
                        if (!window.jQuery) {
                            resolve({ ok: false, reason: "jQuery missing" });
                            return;
                        }
                        let settled = false;
                        let request;
                        const pollStop = async () => {
                            if (settled) return;
                            try {
                                if (window.__uploaderShouldStop && await window.__uploaderShouldStop()) {
                                    settled = true;
                                    request?.abort();
                                    resolve({ ok: false, reason: "cancelled by user" });
                                    return;
                                }
                            } catch (_) { return; }
                            if (!settled) window.setTimeout(pollStop, 200);
                        };
                        window.setTimeout(pollStop, 200);
                        request = window.jQuery
                            .get(url, params)
                            .done((data) => {
                                settled = true;
                                const target = window.jQuery(targetSelector);
                                if (!target.length) {
                                    resolve({ ok: false, reason: `${targetSelector} missing` });
                                    return;
                                }
                                const nodes = window.jQuery.parseHTML(data, document, false);
                                if (nodes) {
                                    target.empty().append(nodes);
                                } else {
                                    target.html(data);
                                }
                                resolve({ ok: true });
                            })
                            .fail((xhr, status, error) => {
                                settled = true;
                                const code = xhr && xhr.status ? ` HTTP ${xhr.status}` : "";
                                const detail = status || error || "unknown";
                                resolve({ ok: false, reason: `${url} failed:${code} ${detail}` });
                            });
                    });

                    const get = async (url, params, targetSelector) => {
                        let last = { ok: false, reason: `${url} was not requested` };
                        for (let attempt = 1; attempt <= 5; attempt += 1) {
                            if (window.__uploaderShouldStop && await window.__uploaderShouldStop()) {
                                return { ok: false, reason: "cancelled by user" };
                            }
                            last = await getOnce(url, params, targetSelector);
                            if (last.ok) {
                                return last;
                            }
                            if (attempt < 5) {
                                await sleep(attempt * 700);
                            }
                        }
                        return last;
                    };

                    const failMissing = (field, expected, select) => ({
                        status: "error",
                        field,
                        expected,
                        available: formatOptions(select),
                    });

                    const tierSelect = document.querySelector("#tierselect");
                    if (!tierSelect) {
                        return { status: "not-site-images" };
                    }

                    const siteId = document.querySelector("#crsite")?.value || "";
                    if (!siteId) {
                        return { status: "error", field: "Site", expected: "site id", available: "<missing #crsite>" };
                    }

                    const logs = [];
                    const priorTier = selectedOption(tierSelect);
                    const priorTierId = priorTier?.id || priorTier?.value || "";
                    const tierOption = findOption(tierSelect, tier);
                    if (!tierOption) {
                        return failMissing("Tier", tier, tierSelect);
                    }
                    setSelect(tierSelect, tierOption, "tier");
                    logs.push(`set Tier = ${tier}`);

                    const selectedTier = selectedOption(tierSelect);
                    const tierId = selectedTier?.id || selectedTier?.value || "";
                    const deptLoad = await get(
                        "/ControlRoom/CRDepartmentPartial",
                        { siteid: siteId, tierid: tierId },
                        "#deptDiv"
                    );
                    if (!deptLoad.ok) {
                        const existingDeptSelect = document.querySelector("#deptselect");
                        if (priorTierId !== tierId || !findOption(existingDeptSelect, department)) {
                            return {
                                status: "error",
                                field: "Department",
                                expected: department,
                                available: deptLoad.reason,
                            };
                        }
                        logs.push(`Department refresh failed but existing options match ${department}; continuing`);
                    }
                    await sleep(50);

                    const deptSelect = document.querySelector("#deptselect");
                    if (!deptSelect) {
                        return { status: "error", field: "Department", expected: department, available: "<missing #deptselect>" };
                    }
                    const deptOption = findOption(deptSelect, department);
                    if (!deptOption) {
                        return failMissing("Department", department, deptSelect);
                    }
                    setSelect(deptSelect, deptOption, "dept");
                    logs.push(`set Department = ${department}`);

                    const selectedDept = selectedOption(deptSelect);
                    const deptId = selectedDept?.id || selectedDept?.value || "";
                    const params = { siteid: siteId, tierid: tierId, deptId };
                    const shiftLoad = await get("/ControlRoom/ImageShiftPartial", params, "#shftDiv");
                    const metricLoad = await get("/ControlRoom/CRMetricPartial", params, "#metricDiv");
                    await sleep(50);

                    const shiftSelect = document.querySelector("#shiftselect");
                    if (!shiftSelect) {
                        return { status: "error", field: "Shift", expected: shift, available: "<missing #shiftselect>" };
                    }
                    if (!shiftLoad.ok && !findOption(shiftSelect, shift)) {
                        return { status: "error", field: "Shift", expected: shift, available: shiftLoad.reason };
                    }
                    if (!shiftLoad.ok) {
                        logs.push(`Shift refresh failed but existing options match ${shift}; continuing`);
                    }
                    const shiftOption = findOption(shiftSelect, shift);
                    if (!shiftOption) {
                        return failMissing("Shift", shift, shiftSelect);
                    }
                    setSelect(shiftSelect, shiftOption, "shiftlist");
                    logs.push(`set Shift = ${shift}`);

                    const metricSelect = document.querySelector("#metricselect");
                    if (!metricSelect) {
                        return { status: "error", field: "Metrics", expected: metric, available: "<missing #metricselect>" };
                    }
                    if (!metricLoad.ok && !findOption(metricSelect, metric)) {
                        return { status: "error", field: "Metrics", expected: metric, available: metricLoad.reason };
                    }
                    if (!metricLoad.ok) {
                        logs.push(`Metrics refresh failed but existing options match ${metric}; continuing`);
                    }
                    const metricOption = findOption(metricSelect, metric);
                    if (!metricOption) {
                        return failMissing("Metrics", metric, metricSelect);
                    }
                    setSelect(metricSelect, metricOption, "metric");
                    logs.push(`set Metrics = ${metric}`);

                    return {
                        status: "ok",
                        logs,
                        ids: { siteId, tierId, deptId },
                    };
                }
                """,
                payload,
            )
        except Exception:
            return False

        self._check_stopped()
        if not isinstance(result, dict):
            return False
        if result.get("status") == "not-site-images":
            return False
        if result.get("status") == "ok":
            for message in result.get("logs", []):
                self.log(f"Row {row.row_number}: {message}")
            return True

        field = str(result.get("field") or "field")
        expected = str(result.get("expected") or "")
        available = str(result.get("available") or "<unknown>")
        message = (
            f"Row {row.row_number}: {field} option '{expected}' is not available after direct refresh. "
            f"Available options: {available}"
        )
        if _is_transient_site_images_error(available):
            raise SiteImagesTransientError(message)
        raise BotUserError(
            message
        )

    def _refresh_department_dependents(self, page: Any) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    async () => {
                        if (!window.jQuery) {
                            return false;
                        }

                        const siteId = document.querySelector("#crsite")?.value || "";
                        const tierOption = document.querySelector("#tierselect option:checked");
                        const deptOption = document.querySelector("#deptselect option:checked");
                        const tierId = tierOption ? tierOption.id || tierOption.value || "" : "";
                        const deptId = deptOption ? deptOption.id || deptOption.value || "" : "";

                        if (!siteId || !tierId || !deptId) {
                            return false;
                        }

                        const loadPartial = (selector, url, params) => new Promise((resolve) => {
                            window.jQuery
                                .get(url, params)
                                .done((data) => {
                                    const target = window.jQuery(selector);
                                    if (!target.length) {
                                        resolve(false);
                                        return;
                                    }
                                    target.html(data);
                                    resolve(true);
                                })
                                .fail(() => resolve(false));
                        });

                        const params = { siteid: siteId, tierid: tierId, deptId };
                        const shiftLoaded = await loadPartial(
                            "#shftDiv",
                            "/ControlRoom/ImageShiftPartial",
                            params
                        );
                        const metricLoaded = await loadPartial(
                            "#metricDiv",
                            "/ControlRoom/CRMetricPartial",
                            params
                        );

                        return shiftLoaded && metricLoaded;
                    }
                    """
                )
            )
        except Exception:
            return False

    def _set_named_field(
        self,
        root: Any,
        page: Any,
        labels: tuple[str, ...],
        value: str,
        row_number: int,
    ) -> None:
        if not value:
            self.log(f"Row {row_number}: {labels[0]} is blank, leaving current value unchanged.")
            return

        field = self._find_field(root, labels)
        if field is None:
            raise BotUserError(f"Row {row_number}: could not find field '{labels[0]}' in Site Images.")

        self._set_field_value(page, field, value, labels[0], row_number)
        self.log(f"Row {row_number}: set {labels[0]} = {value}")

    def _find_field(self, root: Any, labels: tuple[str, ...]) -> Any | None:
        fast_selectors = _fast_field_selectors(labels)
        if fast_selectors:
            try:
                locator = root.locator(", ".join(fast_selectors)).first
                locator.wait_for(state="attached", timeout=250)
                return locator
            except Exception:
                pass

        for label in labels:
            label_pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
            loose_pattern = re.compile(re.escape(label), re.I)

            locators = [
                lambda label_pattern=label_pattern: root.get_by_label(label_pattern).first,
                lambda loose_pattern=loose_pattern: root.get_by_label(loose_pattern).first,
                lambda label=label: root.locator(
                    f"input:not([type=file])[aria-label*='{label}' i], "
                    f"select[aria-label*='{label}' i], "
                    f"textarea[aria-label*='{label}' i], "
                    f"[role='combobox'][aria-label*='{label}' i], "
                    f"input:not([type=file])[placeholder*='{label}' i]"
                ).first,
                lambda label=label: root.locator(_field_xpath(label)).first,
            ]

            for locator_factory in locators:
                try:
                    locator = locator_factory()
                    locator.wait_for(state="attached", timeout=1_000)
                    return locator
                except Exception:
                    continue
        return None

    def _set_field_value(self, page: Any, field: Any, value: str, field_name: str, row_number: int) -> None:
        try:
            tag_name = field.evaluate("element => element.tagName.toLowerCase()")
            input_type = field.evaluate("element => (element.getAttribute('type') || '').toLowerCase()")
            role = field.evaluate("element => (element.getAttribute('role') || '').toLowerCase()")
        except Exception:
            tag_name = ""
            input_type = ""
            role = ""

        if tag_name == "select":
            self._set_select_value(page, field, value, field_name, row_number)
            return

        if tag_name in {"input", "textarea"} and input_type != "file":
            try:
                if field.is_editable(timeout=1_000):
                    field.fill(value)
                    try:
                        field.press("Enter")
                    except Exception:
                        pass
                    self._maybe_choose_option(page, value)
                    return
            except Exception:
                pass

        field.click(timeout=5_000)
        if role == "combobox":
            try:
                field.fill(value)
            except Exception:
                pass
        if not self._maybe_choose_option(page, value):
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass

    def _set_select_value(self, page: Any, field: Any, value: str, field_name: str, row_number: int) -> None:
        try:
            field.wait_for(state="visible", timeout=SELECT_ACTION_TIMEOUT_MS)
        except Exception:
            pass

        option = self._wait_for_select_option(page, field, value, field_name, row_number)
        attempts = self._select_option_attempts(option, value)
        errors: list[Exception] = []
        for kwargs in attempts:
            try:
                field.select_option(timeout=SELECT_ACTION_TIMEOUT_MS, **kwargs)
                return
            except Exception as exc:
                errors.append(exc)

        available = _format_options(self._select_options(field))
        raise BotUserError(
            f"Row {row_number}: could not set {field_name} to '{value}'. "
            f"Available options: {available}"
        ) from (errors[-1] if errors else None)

    def _wait_for_select_option(
        self,
        page: Any,
        field: Any,
        value: str,
        field_name: str,
        row_number: int,
    ) -> dict[str, Any]:
        timeout_ms = _select_option_timeout_ms(field_name)
        elapsed_ms = 0
        waiting_logged = False

        while elapsed_ms <= timeout_ms:
            self._check_stopped()
            options = self._select_options(field)
            option = _matching_option(options, value)
            if option is not None:
                if waiting_logged:
                    self.log(
                        f"Row {row_number}: {field_name} option '{value}' appeared after "
                        f"{_format_ms(elapsed_ms)}."
                    )
                return option

            if not waiting_logged:
                self.log(f"Row {row_number}: waiting for {field_name} option = {value}")
                waiting_logged = True

            page.wait_for_timeout(SELECT_OPTION_POLL_MS)
            elapsed_ms += SELECT_OPTION_POLL_MS

        available = _format_options(options)
        raise SelectOptionUnavailableError(
            f"Row {row_number}: {field_name} option '{value}' did not become available after "
            f"{_format_ms(timeout_ms)}. Available options: {available}"
        )

    def _select_options(self, field: Any) -> list[dict[str, Any]]:
        try:
            raw_options = field.evaluate(
                """
                element => Array.from(element.options || []).map((option, index) => ({
                    index,
                    label: option.label || "",
                    text: option.textContent || "",
                    value: option.value || "",
                    disabled: option.disabled
                }))
                """
            )
        except Exception:
            return []
        if not isinstance(raw_options, list):
            return []
        return [option for option in raw_options if isinstance(option, dict)]

    def _select_option_attempts(self, option: dict[str, Any], requested_value: str) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        option_value = str(option.get("value") or "")
        option_label = str(option.get("label") or "")
        option_text = str(option.get("text") or "")

        for kwargs in (
            {"value": option_value},
            {"label": option_label},
            {"label": option_text},
            {"label": requested_value},
            {"value": requested_value},
        ):
            if not next(iter(kwargs.values())):
                continue
            if kwargs not in attempts:
                attempts.append(kwargs)
        return attempts

    def _maybe_choose_option(self, page: Any, value: str) -> bool:
        exact = re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        option_locators = [
            lambda: page.get_by_role("option", name=exact).first,
            lambda: page.get_by_role("menuitem", name=exact).first,
            lambda: page.get_by_text(exact).first,
            lambda: page.locator(
                "xpath=//*[self::li or self::option or self::div or self::span]"
                f"[normalize-space(.)={xpath_literal(value)}]"
            ).first,
        ]
        for locator_factory in option_locators:
            try:
                option = locator_factory()
                option.wait_for(state="visible", timeout=1_000)
                option.click(timeout=2_000)
                return True
            except Exception:
                continue
        return False

    def _assign_images(self, root: Any, row: UploadRow) -> None:
        for slot, image_path in enumerate(row.image_paths, start=1):
            if image_path is None:
                unused = self._find_file_input(root, slot)
                if unused is not None:
                    unused.set_input_files([])
                continue

            file_input = self._find_file_input(root, slot)
            if file_input is None:
                raise BotUserError(f"Row {row.row_number}: could not find file input for Image {slot}.")

            try:
                self.log(f"Row {row.row_number}: uploading Image {slot} from {image_path.name}.")
                file_input.set_input_files(str(image_path))
                self.log(f"Row {row.row_number}: uploaded Image {slot} = {image_path.name}")
            except Exception as exc:
                raise BotUserError(f"Row {row.row_number}: could not set Image {slot}.") from exc

    def _find_file_input(self, root: Any, slot: int) -> Any | None:
        label = f"Image {slot}"
        locators = [
            lambda: root.get_by_label(re.compile(rf"^\s*image\s*{slot}\s*$", re.I)).first,
            lambda: root.get_by_label(re.compile(rf"image\s*{slot}", re.I)).first,
            lambda: root.locator(
                f"input[type=file][name*='image_{slot}' i], "
                f"input[type=file][id*='image_{slot}' i], "
                f"input[type=file][name*='image{slot}' i], "
                f"input[type=file][id*='image{slot}' i], "
                f"input[type=file][aria-label*='{label}' i]"
            ).first,
            lambda: root.locator(_file_input_xpath(slot)).first,
        ]

        for locator_factory in locators:
            try:
                locator = locator_factory()
                locator.wait_for(state="attached", timeout=700)
                if _is_file_input(locator):
                    return locator
            except Exception:
                continue

        try:
            inputs = root.locator("input[type=file]")
            if inputs.count() >= slot:
                return inputs.nth(slot - 1)
        except Exception:
            pass
        return None

    def _click_upload(self, root: Any, page: Any, row_number: int) -> None:
        upload_locators = [
            lambda: root.get_by_role("button", name=re.compile(r"^\s*upload\s*$", re.I)).first,
            lambda: root.locator(
                "button:has-text('Upload'), input[type=submit][value*='Upload' i], "
                "input[type=button][value*='Upload' i]"
            ).first,
            lambda: root.get_by_text(re.compile(r"^\s*upload\s*$", re.I)).first,
        ]
        for locator_factory in upload_locators:
            try:
                upload = locator_factory()
                upload.wait_for(state="visible", timeout=2_000)
            except Exception:
                continue
            self._check_stopped()
            dialog_count_before = self._dialog_count
            try:
                upload.click(timeout=5_000)
            except Exception as exc:
                raise BotUserError(
                    f"Row {row_number}: upload outcome is uncertain. Check PD Board before retrying."
                ) from exc
            self._wait_for_upload_feedback(page, dialog_count_before)
            return
        raise BotUserError(f"Row {row_number}: could not find the Upload button.")

    def _wait_for_upload_feedback(self, page: Any, dialog_count_before: int) -> None:
        elapsed_ms = 0
        step_ms = 200
        while elapsed_ms < UPLOAD_DIALOG_TIMEOUT_MS:
            if self._dialog_count > dialog_count_before:
                return
            page.wait_for_timeout(step_ms)
            elapsed_ms += step_ms

        self.log(
            "Upload confirmation popup was not detected within "
            f"{UPLOAD_DIALOG_TIMEOUT_MS // 1000}s; continuing."
        )
        try:
            page.wait_for_timeout(UPLOAD_NO_DIALOG_SETTLE_MS)
        except Exception:
            pass

    def _save_debug_output(self, page: Any, reason: str) -> None:
        debug_dir = logs_dir() / "debug"
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.log(f"Could not create debug output directory: {exc}")
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(reason)
        base_path = debug_dir / f"{timestamp}-{slug}"
        screenshot_path = base_path.with_suffix(".png")
        html_path = base_path.with_suffix(".html")
        meta_path = base_path.with_suffix(".txt")
        url, title = self._page_url_and_title(page)

        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            self.log(f"Saved debug screenshot: {screenshot_path}")
        except Exception as exc:
            self.log(f"Could not save debug screenshot: {exc}")

        try:
            html_path.write_text(page.content(), encoding="utf-8")
            self.log(f"Saved debug HTML: {html_path}")
        except Exception as exc:
            self.log(f"Could not save debug HTML: {exc}")

        try:
            meta_path.write_text(
                f"Reason: {reason}\nURL: {url}\nTitle: {title}\n",
                encoding="utf-8",
            )
            self.log(f"Saved debug page info: {meta_path}")
        except Exception as exc:
            self.log(f"Could not save debug page info: {exc}")


def _field_xpath(label: str) -> str:
    lower = label.lower()
    return (
        "xpath=.//*[self::label or self::span or self::div or self::td or self::th]"
        "[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"{xpath_literal(lower)})]"
        "/following::*[self::select or self::input[not(@type='file')] or self::textarea or @role='combobox'][1]"
    )


def _fast_field_selectors(labels: tuple[str, ...]) -> list[str]:
    names = {_normalise_option(label) for label in labels}
    if "tier" in names:
        return [
            "select#tierselect",
            "select[name='tierselect']",
            "select.tier",
        ]
    if "department" in names:
        return [
            "select#deptselect",
            "select[name='deptselect']",
            "select.dept",
        ]
    if "shift" in names:
        return [
            "select#shiftselect",
            "select[name='shiftselect']",
            "#shftDiv select",
            'select[onchange*="shift"]',
        ]
    if "metrics" in names or "metric" in names:
        return [
            "select#metricselect",
            "select[name='metricselect']",
            "#metricDiv select",
        ]
    return []


def _site_images_select_state(root: Any) -> tuple[tuple[str, str], ...]:
    state: list[tuple[str, str]] = []
    fields = [
        ("Tier", ("Tier",)),
        ("Department", ("Department",)),
        ("Shift", ("Shift",)),
        ("Metrics", ("Metrics", "Metric")),
    ]
    for name, labels in fields:
        selectors = _fast_field_selectors(labels)
        if not selectors:
            state.append((name, "<missing>"))
            continue
        try:
            locator = root.locator(", ".join(selectors))
            if locator.count() == 0:
                state.append((name, "<missing>"))
                continue
            state.append((name, _field_display_value(locator.first)))
        except Exception:
            state.append((name, "<missing>"))
    return tuple(state)


def _site_images_selects_ready(state: tuple[tuple[str, str], ...]) -> bool:
    if len(state) != 4:
        return False
    return all(value not in {"<missing>", "<blank>"} for _, value in state)


def _file_input_xpath(slot: int) -> str:
    label = f"image {slot}"
    return (
        "xpath=.//*[self::label or self::span or self::div or self::td or self::th]"
        "[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"{xpath_literal(label)})]"
        "/following::input[@type='file'][1]"
    )


def _is_file_input(locator: Any) -> bool:
    try:
        return bool(
            locator.evaluate(
                "element => element.tagName.toLowerCase() === 'input' "
                "&& (element.getAttribute('type') || '').toLowerCase() === 'file'"
            )
        )
    except Exception:
        return False


def _matching_option(options: list[dict[str, Any]], value: str) -> dict[str, Any] | None:
    expected = _normalise_option(value)
    enabled_options = [option for option in options if not option.get("disabled")]
    for key in ("label", "text", "value"):
        for option in enabled_options:
            if _normalise_option(str(option.get(key) or "")) == expected:
                return option
    return None


def _field_display_value(field: Any) -> str:
    for value in _field_current_values(field):
        cleaned = re.sub(r"\s+", " ", value).strip()
        if cleaned:
            return cleaned
    return "<blank>"


def _field_current_values(field: Any) -> list[str]:
    try:
        values = field.evaluate(
            """
            element => {
                const tagName = element.tagName.toLowerCase();
                if (tagName === "select") {
                    const option = element.options ? element.options[element.selectedIndex] : null;
                    return [
                        element.value || "",
                        option ? option.label || "" : "",
                        option ? option.textContent || "" : ""
                    ];
                }
                return [element.value || element.textContent || ""];
            }
            """
        )
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _format_options(options: list[dict[str, Any]], *, limit: int = 20) -> str:
    labels: list[str] = []
    for option in options:
        if option.get("disabled"):
            continue
        label = str(option.get("label") or option.get("text") or option.get("value") or "").strip()
        if not label:
            continue
        labels.append(re.sub(r"\s+", " ", label))
    if not labels:
        return "<none>"
    visible = labels[:limit]
    suffix = f", ... (+{len(labels) - limit} more)" if len(labels) > limit else ""
    return ", ".join(visible) + suffix


def _normalise_option(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _select_option_timeout_ms(field_name: str) -> int:
    if _normalise_option(field_name) == "department":
        return DEPARTMENT_SELECT_OPTION_TIMEOUT_MS
    return DEFAULT_SELECT_OPTION_TIMEOUT_MS


def _is_transient_site_images_error(value: str) -> bool:
    lowered = value.casefold()
    transient_markers = (
        "<missing #",
        " failed:",
        "jquery missing",
        "was not requested",
        "timeout",
        "network",
        "error",
    )
    return any(marker in lowered for marker in transient_markers)


def _format_ms(value: int) -> str:
    seconds = value / 1000
    if seconds.is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:.1f}s"


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "debug"


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in parts) + ")"
