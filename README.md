# PD Uploader

A Python-based desktop application designed to automate image uploads to multiple Performance Dialogue Boards.

## Technical overview

The application uses Python for processing and application logic, PySide6 (Qt for Python) for the graphical interface, and Playwright for browser automation.

It interacts directly with website elements and JavaScript functions, rather than relying on screen coordinates or visual recognition.

CSV files define the destination, form parameters and image mappings. Each upload job operates within its own isolated, managed Microsoft Edge session.

## Key features

- Sequential uploads across multiple PD Boards.
- Independent CSV tabs and browser sessions.
- Background uploading using minimized browser windows.
- Live progress tracking and elapsed time.
- Safe pause, resume, skip and cancellation mechanisms.
- Destination validation and upload result verification.
- Error classification, recovery and diagnostic logging.
- Support for local and synchronized SharePoint/OneDrive file paths.

## Technology stack

- Python 3.11
- PySide6 Essentials
- Playwright
- JavaScript
- PyInstaller
- GitHub Actions
- Python unittest

## Architecture and security

The application operates locally and uses existing website functionality within authenticated browser sessions. It does not require direct database access or server-side modifications.

Managed browser sessions are isolated from the user's existing browser profiles. No credentials, private endpoints or operational data are included in the repository.

## Distribution

The Windows application is distributed as a standalone folder-based executable package, built using PyInstaller and GitHub Actions.

## Current release — v0.3.1

A redesigned desktop interface, independent upload queues, improved session management and safer upload recovery.

See the release notes for detailed implementation changes, build instructions and validation results.

## Development status

Automated tests cover core application logic, upload mapping, session management, UI interactions and packaging.

Production environment validation is ongoing.
