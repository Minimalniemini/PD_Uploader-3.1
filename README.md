PD Uploader 0.3.1
A separate release with a completely replaced desktop interface based on the approved compact prototype. The existing CSV parsing, form mapping, sequential row uploads and safe result accounting remain the foundation. Earlier 0.2.0 and 0.3.0 packages are not overwritten.

One contextual button
Load one or more CSV files and select rows.
Click Open PD Board. Only the assigned login page is opened; the uploader does not click Control Room, open a modal or guess that login is complete.
Log in inside that managed Edge window. Return to the uploader and click the same button, now I’m logged in.
The uploader first minimizes and verifies the assigned Edge window, then prepares Control Room and its form. If minimization fails, preparation stops instead of falling back to visible navigation.
With multiple CSVs, click Open PD Board again for the next required session, then I’m logged in. Previously prepared sessions stay intact.
When ready, that same button becomes Start upload / Start all uploads, then Pause upload, then Resume upload. It stays in the same position throughout.
Manual login confirmation is a prerequisite, not a reason to submit blindly: destination and form checks remain before any upload. Login expiry uses the same explicit Open / I’m logged in flow without discarding confirmed results. The browser never opens itself automatically in the middle of an upload run to recover a closed session.

Interface
The Tkinter interface is replaced with Qt for Python (PySide6 Essentials). The window stays compact (1080 × 550 logical pixels initially) and resizable. It uses flat white rounded panels, the approved yellow/graphite palette, thin table separators, blue checkboxes, pale-yellow selected tabs, a separate execution underline, a circular progress display, and a collapsed Activity log.

All production table information remains available: row number, tier, department, shift, metric, image count and status. Tabs show independent actual CSV data; switching the viewed tab never changes execution. Row inspection, per-job selection, safe skip/cancel/retry and diagnostics are retained. No theme settings, dashboard, sample-data controls or company branding are introduced.

The application and Windows executable use a simple PD monogram icon. Both runtime assets and the EXE icon are explicitly packaged; Windows version metadata identifies PD Uploader instead of the default Python application.

Upload safety retained
One server operation remains one CSV row with up to six image slots. No unverified partial-row uploads.
Slow page, selector and upload-confirmation responses do not automatically start the next row or cause another Upload click.
Pause stops before the next row; an already submitted row settles first. Resume does not repeat confirmed rows.
Unknown outcomes never enter automatic retry. The exceptional Review upload result action requires the user to check all saved images before marking that row successful.
Each CSV owns an explicit managed page and separate temporary Edge profile. Other user Edge windows/profiles are neither attached to nor closed.
Minimization uses the window ID of that managed page, checks the resulting minimized state, and does not use the foreground window, OS mouse or a global process kill.
The fixed progress denominator counts selected images, not clicks. Failed, skipped and cancelled work does not count as successful. Active elapsed time is monotonic and freezes on pause/reauthentication/completion.
Missing/unreadable/changed images are detected before submission; the entire affected row is withheld. Retry is restricted to known pre-submit failures.
CSV
The schema and path handling are preserved:

enabled,page_url,tier,department,shift,metric,image_1,image_2,image_3,image_4,image_5,image_6
UTF-8 with optional BOM, comma-separated. Only .jpg, case-insensitive. Relative image paths resolve from the CSV directory. Enabled rows require nonblank destination/tier/department/shift/metric. Mappings are snapshotted at load and file availability/signatures/size/modification state are rechecked before submission.

Local approved synced OneDrive/SharePoint paths use the filesystem. Direct SharePoint URLs and Graph/auth integration are not added. Each row’s destination is used and checked against the existing page evidence; unknown or mismatched destinations do not receive uploads.

Building Windows
Install Python 3.11 and use BUILD-WINDOWS.cmd, or:

py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m unittest discover -s tests
pyinstaller --noconfirm control_room_uploader.spec
The folder distribution is dist/PDUploader-0.3.1/, containing PDUploader-0.3.1.exe and _internal. Distribute the complete folder; the installed approved Microsoft Edge is used. No onefile executable, browser security bypass or user-profile modification is introduced.

The included workflow supports source files at repository root or inside PD_Uploader_0.3.1/. It runs unit/offscreen UI tests, a Windows-only isolated managed Edge minimization test, builds the folder, and smoke-tests the packaged EXE starting/closing without login or uploads. The artifact is PDUploader-0.3.1-windows.

A visible BUILD_ON_GITHUB.yml copy is supplied alongside the source package for adding/updating the workflow through GitHub’s editor if dragging the hidden .github folder is rejected. Its repository destination must remain .github/workflows/build-windows-release.yml.

Validation and remaining checks
68 local tests pass, including the inherited upload mapping, independent jobs, accounting, row-boundary pause/resume, safe retry, slow-server waits, manual login ordering, minimization-before-preparation, refusal to continue visibly if minimization fails, and real offscreen Qt control interaction. The redesigned UI was rendered and compared against the provided reference using generic fixtures. A local macOS folder build/startup smoke test is also used to check packaging.

Real Windows/corporate Edge behavior, actual login redirects, server popup wording and background site behavior still require acceptance testing. The Windows workflow includes a real two-window Edge minimization check using only disposable profiles and local HTML; no production uploads are part of that check. A mocked test passing is not proof of actual server behavior.

Start with one approved test row on Windows, then two CSVs. Confirm that Control Room only opens after I’m logged in, Edge remains minimized, the correct images are saved, and pause/resume preserves results.

Privacy and resources
The app starts with an empty queue. It remembers only the picker directory. Local rotating diagnostics redact addresses, credential fields and common personal paths; no default raw HTML/screenshots or storage-state exports are created. Review logs before external sharing because operational values/filenames can remain. A minimal result summary is informational, not automatic crash recovery or proof of an unconfirmed write.

0.3.1 uses a separate PDUploader031 application-data directory and temporary job profiles. Temporary profiles are removed after managed sessions close; OS file locks or crashes can delay cleanup, without killing unrelated browser processes.

Qt/PySide6 Essentials and shiboken are third-party dependencies, distributed dynamically with their package metadata/notices. The PD icon is a simple generated typographic asset. No operational data, private hosts, real CSV mappings or credentials belong in this repository.
