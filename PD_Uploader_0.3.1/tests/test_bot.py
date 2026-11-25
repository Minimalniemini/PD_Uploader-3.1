from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from typing import Any

from control_room_uploader.bot import (
    BotUserError,
    ControlRoomBot,
    SelectOptionUnavailableError,
    SiteImagesTransientError,
    _fast_field_selectors,
    _format_options,
    _matching_option,
)
from control_room_uploader.models import UploadRow


class FakePage:
    url = "https://example.invalid/?siteid=33"

    def __init__(self) -> None:
        self.bring_to_front_count = 0

    def is_closed(self) -> bool:
        return False

    def bring_to_front(self) -> None:
        self.bring_to_front_count += 1


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class RunFlowBot(ControlRoomBot):
    def __init__(self, page: FakePage, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)
        self._context = FakeContext(page)
        self.calls: list[str] = []

    def _log_current_page(self, page: Any) -> None:
        self.calls.append("log_current_page")

    def _check_access_denied(self, page: Any) -> None:
        self.calls.append("check_access_denied")

    def _ensure_control_room_landing(self, page: Any) -> None:
        self.calls.append("ensure_control_room_landing")

    def _open_site_images_panel(self, page: Any) -> object:
        self.calls.append("open_site_images_panel")
        return object()

    def _set_named_field(
        self,
        root: Any,
        page: Any,
        labels: tuple[str, ...],
        value: str,
        row_number: int,
    ) -> None:
        self.calls.append(f"set_{labels[0].lower()}")

    def _assign_images(self, root: Any, row: UploadRow) -> None:
        self.calls.append("assign_images")

    def _click_upload(self, root: Any, page: Any, row_number: int) -> None:
        self.calls.append("click_upload")


class FakeFileInput:
    def __init__(self) -> None:
        self.files: list[str] = []

    def set_input_files(self, value: str) -> None:
        self.files.append(value)


class FakeModalPage:
    def __init__(self) -> None:
        self.evaluate_calls: list[str] = []

    def evaluate(self, script: str) -> str:
        self.evaluate_calls.append(script)
        return "patched"


class FakeWaitPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, value: int) -> None:
        self.waits.append(value)


class ShiftRefreshPage(FakeWaitPage):
    pass


class DelayedSelectField:
    def __init__(self, available_after: int) -> None:
        self.available_after = available_after
        self.calls = 0

    def evaluate(self, script: str) -> list[dict[str, object]]:
        self.calls += 1
        if self.calls <= self.available_after:
            return [{"label": "DAYS", "text": "DAYS", "value": "ShiftDays", "disabled": False}]
        return [{"label": "24 Hrs", "text": "24 Hrs", "value": "Shift1", "disabled": False}]


class ImageAssignBot(ControlRoomBot):
    def __init__(self, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)
        self.requested_slots: list[int] = []
        self.file_input = FakeFileInput()

    def _find_file_input(self, root: Any, slot: int) -> FakeFileInput:
        self.requested_slots.append(slot)
        return self.file_input


class ShiftRetryBot(ControlRoomBot):
    def __init__(self, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)
        self.calls: list[str] = []
        self.shift_attempts = 0

    def _set_named_field(
        self,
        root: Any,
        page: Any,
        labels: tuple[str, ...],
        value: str,
        row_number: int,
    ) -> None:
        self.calls.append(f"set_{labels[0].lower()}")
        if labels[0] == "Shift":
            self.shift_attempts += 1
            if self.shift_attempts == 1:
                raise SelectOptionUnavailableError(
                    "Row 18: Shift option 'ALL' did not become available after 25s. "
                    "Available options: Days, Nights"
                )

    def _refresh_department_dependents(self, page: Any) -> bool:
        self.calls.append("refresh_department_dependents")
        return True

    def _patch_site_images_change_func(self, page: Any) -> None:
        self.calls.append("patch_change_func")


class PatchBot(ControlRoomBot):
    def __init__(self, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)


class DirectSiteImagesPage:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.script = ""
        self.payload: dict[str, str] = {}

    def evaluate(self, script: str, payload: dict[str, str]) -> dict[str, object]:
        self.script = script
        self.payload = payload
        return self.result


class DirectOnlyBot(ControlRoomBot):
    def __init__(self, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)

    def _set_named_field(
        self,
        root: Any,
        page: Any,
        labels: tuple[str, ...],
        value: str,
        row_number: int,
    ) -> None:
        raise AssertionError("direct Site Images selectors should be used")


class RecoveringRunBot(ControlRoomBot):
    def __init__(self, page: FakePage, log_messages: list[str]) -> None:
        super().__init__(Path("."), log_messages.append)
        self._context = FakeContext(page)
        self.calls: list[str] = []
        self.field_attempts = 0

    def _log_current_page(self, page: Any) -> None:
        self.calls.append("log_current_page")

    def _check_access_denied(self, page: Any) -> None:
        self.calls.append("check_access_denied")

    def _ensure_control_room_landing(self, page: Any) -> None:
        self.calls.append("ensure_control_room_landing")

    def _open_site_images_panel(self, page: Any) -> object:
        self.calls.append("open_site_images_panel")
        return object()

    def _reset_site_images_panel(self, page: Any, row_number: int) -> object:
        self.calls.append(f"reset_{row_number}")
        return object()

    def _set_row_fields(self, panel: Any, page: Any, row: UploadRow) -> None:
        self.field_attempts += 1
        self.calls.append(f"set_fields_{self.field_attempts}")
        if self.field_attempts == 1:
            raise SiteImagesTransientError("missing #shiftselect")

    def _assign_images(self, root: Any, row: UploadRow) -> None:
        self.calls.append("assign_images")

    def _click_upload(self, root: Any, page: Any, row_number: int) -> None:
        self.calls.append("click_upload")

    def _wait_for_site_images_after_upload(self, page: Any) -> None:
        self.calls.append("wait_after_upload")


def _row(row_number: int) -> UploadRow:
    return UploadRow(
        row_number=row_number,
        enabled=True,
        page_url="https://example.invalid/?siteid=33",
        tier="Tier 1",
        department="Dept",
        shift="Shift",
        metric="Metric",
        image_paths=(None, None, None, None, None, None),
    )


class BotRunFlowTests(unittest.TestCase):
    def test_run_uses_existing_start_page_without_controlroommain_url_detection(self) -> None:
        page = FakePage()
        log_messages: list[str] = []
        bot = RunFlowBot(page, log_messages)
        row = _row(2)

        bot.run_rows([row])

        self.assertEqual(page.bring_to_front_count, 1)
        self.assertIn("ensure_control_room_landing", bot.calls)
        self.assertIn("open_site_images_panel", bot.calls)
        self.assertTrue(
            any("using current browser page" in message for message in log_messages)
        )
        self.assertTrue(any(message.startswith("Run started at:") for message in log_messages))
        self.assertTrue(any(message.startswith("Total upload time:") for message in log_messages))

    def test_assign_images_clears_unused_slots_to_avoid_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "one.jpg"
            image.write_bytes(b"fake jpg")
            log_messages: list[str] = []
            bot = ImageAssignBot(log_messages)
            row = UploadRow(
                row_number=2,
                enabled=True,
                page_url="https://example.invalid/?siteid=33",
                tier="Tier 1",
                department="Dept",
                shift="Shift",
                metric="Metric",
                image_paths=(image, None, None, None, None, None),
            )

            bot._assign_images(object(), row)

            self.assertEqual(bot.requested_slots, [1, 2, 3, 4, 5, 6])
            self.assertEqual(bot.file_input.files, [str(image), [], [], [], [], []])
            self.assertFalse(any("cleared Image" in message for message in log_messages))

    def test_set_row_fields_refreshes_shift_partial_when_shift_options_are_stale(self) -> None:
        log_messages: list[str] = []
        bot = ShiftRetryBot(log_messages)
        page = ShiftRefreshPage()
        row = UploadRow(
            row_number=18,
            enabled=True,
            page_url="https://example.invalid/?siteid=83",
            tier="Tier 1",
            department="CHILL",
            shift="ALL",
            metric="DELIVERY",
            image_paths=(None, None, None, None, None, None),
        )

        bot._set_row_fields(object(), page, row)

        self.assertEqual(
            bot.calls,
            [
                "set_tier",
                "set_department",
                "set_shift",
                "refresh_department_dependents",
                "patch_change_func",
                "set_shift",
                "set_metrics",
            ],
        )
        self.assertEqual(page.waits, [500])
        self.assertTrue(
            any("requesting fresh Shift/Metric options and retrying Shift once" in message for message in log_messages)
        )

    def test_set_row_fields_uses_direct_site_images_select_chain_when_available(self) -> None:
        log_messages: list[str] = []
        bot = DirectOnlyBot(log_messages)
        page = DirectSiteImagesPage(
            {
                "status": "ok",
                "logs": [
                    "set Tier = Tier 1",
                    "set Department = CHILL",
                    "set Shift = ALL",
                    "set Metrics = SAFETY",
                ],
            }
        )
        row = UploadRow(
            row_number=8,
            enabled=True,
            page_url="https://example.invalid/?siteid=83",
            tier="Tier 1",
            department="CHILL",
            shift="ALL",
            metric="SAFETY",
            image_paths=(None, None, None, None, None, None),
        )

        bot._set_row_fields(object(), page, row)

        self.assertEqual(page.payload["department"], "CHILL")
        self.assertIn("/ControlRoom/CRDepartmentPartial", page.script)
        self.assertIn("/ControlRoom/ImageShiftPartial", page.script)
        self.assertIn("parseHTML(data, document, false)", page.script)
        self.assertIn("shiftlist", page.script)
        self.assertTrue(any("Row 8: set Shift = ALL" in message for message in log_messages))

    def test_set_row_fields_reports_direct_site_images_option_error(self) -> None:
        log_messages: list[str] = []
        bot = DirectOnlyBot(log_messages)
        page = DirectSiteImagesPage(
            {
                "status": "error",
                "field": "Shift",
                "expected": "ALL",
                "available": "Days, Nights",
            }
        )
        row = UploadRow(
            row_number=18,
            enabled=True,
            page_url="https://example.invalid/?siteid=83",
            tier="Tier 1",
            department="CHILL",
            shift="ALL",
            metric="DELIVERY",
            image_paths=(None, None, None, None, None, None),
        )

        with self.assertRaises(BotUserError) as raised:
            bot._set_row_fields(object(), page, row)

        self.assertIn("Row 18: Shift option 'ALL' is not available after direct refresh", str(raised.exception))

    def test_set_row_fields_marks_missing_site_images_select_as_transient(self) -> None:
        log_messages: list[str] = []
        bot = DirectOnlyBot(log_messages)
        page = DirectSiteImagesPage(
            {
                "status": "error",
                "field": "Shift",
                "expected": "Nights",
                "available": "<missing #shiftselect>",
            }
        )
        row = UploadRow(
            row_number=55,
            enabled=True,
            page_url="https://example.invalid/?siteid=83",
            tier="Tier 1",
            department="PICK",
            shift="Nights",
            metric="SAFETY",
            image_paths=(None, None, None, None, None, None),
        )

        with self.assertRaises(SiteImagesTransientError):
            bot._set_row_fields(object(), page, row)

    def test_run_resets_modal_and_retries_same_row_for_transient_site_images_state(self) -> None:
        page = FakePage()
        log_messages: list[str] = []
        bot = RecoveringRunBot(page, log_messages)

        bot.run_rows([_row(55)])

        self.assertIn("reset_55", bot.calls)
        self.assertEqual(bot.field_attempts, 2)
        self.assertIn("click_upload", bot.calls)
        self.assertTrue(any("retrying this row" in message for message in log_messages))

    def test_matching_option_accepts_visible_text_label_or_value(self) -> None:
        options = [
            {"label": "Select", "text": "Select", "value": "", "disabled": False},
            {"label": "", "text": " Warehouse ", "value": "2512", "disabled": False},
            {"label": "Dept One", "text": "Dept One", "value": "Dept1", "disabled": False},
        ]

        self.assertEqual(_matching_option(options, "WAREHOUSE"), options[1])
        self.assertEqual(_matching_option(options, "Dept One"), options[2])
        self.assertEqual(_matching_option(options, "Dept1"), options[2])

    def test_format_options_lists_available_values_for_debugging(self) -> None:
        options = [
            {"label": "Select", "text": "Select", "value": "", "disabled": False},
            {"label": "", "text": " Warehouse ", "value": "2512", "disabled": False},
            {"label": "Hidden", "text": "Hidden", "value": "hidden", "disabled": True},
        ]

        self.assertEqual(_format_options(options), "Select, Warehouse")

    def test_fast_field_selectors_cover_site_images_dropdowns(self) -> None:
        self.assertIn("select#tierselect", _fast_field_selectors(("Tier",)))
        self.assertIn("select#deptselect", _fast_field_selectors(("Department",)))
        self.assertIn("select#shiftselect", _fast_field_selectors(("Shift",)))
        self.assertIn("select#metricselect", _fast_field_selectors(("Metrics", "Metric")))

    def test_patch_site_images_change_func_maps_shift_to_shiftlist(self) -> None:
        page = FakeModalPage()
        log_messages: list[str] = []
        bot = PatchBot(log_messages)

        bot._patch_site_images_change_func(page)

        self.assertEqual(len(page.evaluate_calls), 1)
        self.assertIn('item === "shift" ? "shiftlist" : item', page.evaluate_calls[0])
        self.assertTrue(
            any("changeFunc patch for shift -> shiftlist" in message for message in log_messages)
        )

    def test_wait_for_select_option_reacts_when_delayed_option_appears(self) -> None:
        page = FakeWaitPage()
        field = DelayedSelectField(available_after=2)
        log_messages: list[str] = []
        bot = ControlRoomBot(Path("."), log_messages.append)

        option = bot._wait_for_select_option(page, field, "24 Hrs", "Shift", 7)

        self.assertEqual(option["label"], "24 Hrs")
        self.assertEqual(page.waits, [100, 100])
        self.assertTrue(
            any("Shift option '24 Hrs' appeared after 0.2s" in message for message in log_messages)
        )


if __name__ == "__main__":
    unittest.main()
