# 0.3.1

- Replaced the entire Tkinter GUI with a flat Qt desktop interface based on the approved prototype.
- Preserved the compact window, two-panel layout, all production table fields and independent CSV job state.
- Kept a single contextual button for Open PD Board → I’m logged in → Start → Pause → Resume.
- Split opening login from preparing Control Room. No automatic navigation based on inferred login completion.
- Added verified minimization of only the assigned Edge window before any Control Room preparation or upload work.
- Retained the existing form-field/file-slot engine, sequential writes, confirmation waits and safe unknown-outcome handling.
- Replaced the default Python/feather icon with a minimal PD monogram in runtime and Windows EXE packaging.
- Added real offscreen Qt interaction tests, manual login/minimization ordering tests and a Windows-only isolated Edge smoke test.
- Kept a folder-based executable and separate application data; earlier packages remain fallback versions.
