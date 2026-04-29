# Goodvibes Agent Instructions

This repository is a Sublime Text 4 plugin written entirely in Python. It provides inline LLM assistance with real-time text streaming.

## Architecture & Conventions
- **Dependency-Free**: The project relies exclusively on the Python standard library. Do not introduce external dependencies (like `requests` or anything requiring `pip`). It uses `urllib.request` for network calls.
- **Entrypoint**: `goodvibes.py` contains the entirety of the plugin's logic, including commands, event listeners, and the HTTP request handler.
- **Concurrency & UI Updates**: Network requests and SSE stream processing run in background daemon threads (`threading.Thread`) to prevent freezing the editor. Any UI modifications (inserting text, rendering phantoms, updating state) MUST be dispatched back to the main UI thread using `sublime.set_timeout(callback, 0)`.
- **Text Tracking**: Because text streams in chunk-by-chunk and users can edit the document concurrently, the plugin tracks insertion points using hidden `sublime.Region`s (`self.view.add_regions(...)`).
- **Undo/Redo Interception**: Since streamed tokens appear as hundreds of distinct `insert` commands, the plugin globally hijacks `undo` and `redo` via `GoodvibesUndoInterceptor` to bulk-revert entire AI responses at once. 
- **Global State**: Concurrent requests (e.g. multi-cursor generation) are tracked in the global `active_tasks` dictionary, keyed by `view.id()`.

## Build & Testing
- **Packaging**: To bundle the plugin for distribution, run `./bundle.sh`. This creates `Goodvibes.sublime-package` in the root directory.
- **No Test Suite**: There is currently no automated test suite. Verification of changes requires manually installing the `.sublime-package` into Sublime Text 4's `Installed Packages` directory and observing the behavior.
