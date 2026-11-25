# 0.3.1 acceptance checks

Use approved generic test rows first; never upload operational CSVs, addresses, images or logs to GitHub.

1. Build with the included Windows workflow. Confirm all tests, the isolated two-window Edge minimization check and EXE startup smoke test pass. Extract the complete new application folder.
2. Check the PD icon in the title/taskbar/EXE and the compact flat UI. Compare against the approved prototype: no beveled table headers, native feather icon, oversized dashboard or forced maximization.
3. Open one CSV. Click Open PD Board, log in in Edge, and wait without touching the uploader. It must not navigate to Control Room on its own.
4. Return to the uploader. The same button must say I’m logged in. Click it. The assigned Edge must minimize first, and Control Room preparation must happen only afterward.
5. Start one approved row. Edge must stay minimized. Validate the actual server confirmation wording and saved images. If the popup is not recognized, review the saved result rather than blindly retrying.
6. Open two different CSVs. Prepare each explicitly, preserving the prior session. Change the viewed tab during execution; rows/selections/statuses change, execution stays with the original active job.
7. Exercise Back while awaiting login/preparing. Prepared sessions and queue contents must remain intact. If minimization is blocked by policy, no visible fallback navigation may occur.
8. Test slow responses over a minute, pause before/during submit, resume, skip while viewing another tab, cancel and exit cancellation. Retain confirmed results and the original denominator.
9. Close a managed browser or expire its login. Open PD Board → I’m logged in must be required again, with progress retained and no automatic repeat of a possible completed upload.
10. Leave unrelated personal Edge windows open. Only the assigned managed window may minimize/restore/close. Test approved synced paths and inspect sanitized diagnostic exports before sharing externally.
