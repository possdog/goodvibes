import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

import sublime
import sublime_plugin

# Tuning constants
# How often (in seconds) the streaming buffer is flushed to the UI thread.
FLUSH_INTERVAL = 0.1
# How often (in milliseconds) the phantom animation loop re-queues itself.
ANIMATION_INTERVAL = 500
# Default timeout (in seconds) for the initial HTTP connection and reads.
# Prevents the background thread from hanging indefinitely if a server stalls.
HTTP_TIMEOUT = 30

# Global state to track active tasks per view
# Key: view.id(), Value: dict of task_id -> {
#   "cancel": False,       # Flag used to signal the LLM thread to abort early
#   "status": "thinking",  # Used to determine the phantom text (Thinking vs Generating)
#   "view": view,          # Reference to the Sublime Text view where this task was initiated
#   "phantom_set": PhantomSet # Sublime Text PhantomSet to render inline "ghost" text
# }
# We use this global dictionary so that if the user invokes multiple commands
# across different files (or multi-cursor in the same file), we can track
# and manage them independently without them colliding.
#
# Thread safety: active_tasks is accessed from both the main UI thread and
# background daemon threads (one per LLM request). All reads and writes must
# be guarded by active_tasks_lock to prevent race conditions.
active_tasks = {}
active_tasks_lock = threading.Lock()

# A flag used by our custom Undo/Redo commands to signal to our EventListener
# that a bulk undo/redo operation is currently in progress. This prevents the
# EventListener from intercepting its own undo/redo commands and causing an infinite loop.
is_bulk_undoing = False

# A flag to ensure we only have one animation loop running globally at a time.
# Running multiple loops would cause the UI to redraw unnecessarily and potentially stutter.
# Note: is_animating is only ever read and written from the main UI thread (both
# animate_phantoms via set_timeout and update_phantoms run on the UI thread), so it
# does not require active_tasks_lock despite being a module-level global.
is_animating = False


def get_settings():
    """Retrieves the Goodvibes package settings."""
    return sublime.load_settings("goodvibes.sublime-settings")


def get_api_key():
    """
    Retrieves the API key from settings, preferring 'api_key' over 'api_key_var'.
    If 'api_key_var' is set, it fetches the value from the corresponding environment variable.
    Returns the resolved API key or None if neither is configured.
    """
    settings = get_settings()
    api_key = settings.get("api_key")
    api_key_var = settings.get("api_key_var")

    # Prefer api_key if it's set
    if api_key and str(api_key).strip():
        return str(api_key).strip()

    # Fall back to api_key_var environment variable
    if api_key_var and str(api_key_var).strip():
        env_var_name = str(api_key_var).strip()
        env_value = os.environ.get(env_var_name)
        if env_value:
            return env_value.strip()

    return None


def animate_phantoms():
    """
    A self-scheduling loop that animates the inline phantoms (the 'ghost' text)
    while LLM tasks are active.

    Why: Since TTFT (Time To First Token) can take several seconds, a static
    indicator feels unresponsive. By toggling the icon (✦/✧) every 500ms,
    we assure the user that the background process hasn't frozen.
    """
    global is_animating
    with active_tasks_lock:
        if not active_tasks:
            is_animating = False
            return
        # Snapshot under the lock so background threads can safely mutate
        # active_tasks while we iterate and render phantoms below.
        snapshot = list(active_tasks.items())

    # Calculate a tick (0 or 1) based on the current time to toggle the icon
    tick = int(time.time() * 2) % 2
    icon = "✦" if tick == 0 else "✧"

    # Iterate through all tasks in all views to update their phantoms
    for view_id, tasks in snapshot:
        for task_id, task in list(tasks.items()):
            view = task.get("view")

            # If the view was closed or is no longer valid, skip it.
            # The cleanup_task handles full removal later.
            if not view or not view.is_valid():
                continue

            # Optimization: Skip rendering cycles for views that are in background tabs
            if view.window() and view.window().active_view() != view:
                continue

            phantom_set = task.get("phantom_set")
            if not phantom_set:
                continue

            # If the task was cancelled by the user (e.g., via Cmd+Z), clear its phantom.
            if task.get("cancel"):
                phantom_set.update([])
                continue

            # Retrieve the region we are tracking for this task.
            # Sublime Text dynamically shifts this region if the user edits text before it.
            region_key = f"goodvibes_target_{task_id}"
            regions = view.get_regions(region_key)
            if not regions:
                continue

            region = regions[0]

            # Place the phantom at the very end of the tracked region.
            # This ensures it always trails the text as the LLM streams characters in.
            phantom_region = sublime.Region(region.end(), region.end())

            # Determine the state text based on whether the first chunk has arrived
            is_thinking = task.get("status") == "thinking"
            state_text = "Thinking" if is_thinking else "Generating"

            # Create an HTML-formatted phantom. We use a CSS color variable matching
            # the user's theme foreground, but with 50% opacity and italicized
            # to give it a distinct "ghost" appearance.
            content = f'<body id="goodvibes"><span style="color: color(var(--foreground) alpha(0.5)); font-style: italic; padding-left: 0.5rem;">{icon} {state_text}...</span></body>'

            # Render the phantom inline, inserting it into the view layout without modifying the buffer.
            phantom = sublime.Phantom(phantom_region, content, sublime.LAYOUT_INLINE)
            phantom_set.update([phantom])

    # Re-queue the animation loop to run again
    with active_tasks_lock:
        has_tasks = bool(active_tasks)
    if has_tasks:
        sublime.set_timeout(animate_phantoms, ANIMATION_INTERVAL)
    else:
        is_animating = False


def update_phantoms(view):
    """
    Kicks off the global phantom animation loop if it isn't already running.
    """
    global is_animating
    with active_tasks_lock:
        should_start = bool(active_tasks) and not is_animating
        if should_start:
            is_animating = True
    if should_start:
        animate_phantoms()


class GoodvibesInvokeAiCommand(sublime_plugin.TextCommand):
    """
    The main command triggered by the user (via Command Palette, Menu, or Keybind).
    Initiates an LLM request for each current cursor/selection.
    """

    def run(self, edit):
        settings = get_settings()
        api_key = get_api_key()
        api_endpoint = settings.get("api_endpoint")
        model_id = settings.get("model_id")
        system_prompt = settings.get("system_prompt") or ""
        api_parameters = settings.get("api_parameters", {})

        if not api_key or not model_id:
            sublime.error_message(
                "Goodvibes: Please set your api_key (or api_key_var) and model_id in the Goodvibes settings."
            )
            return

        # Default to OpenAI's endpoint if none is provided.
        api_endpoint = (
            str(api_endpoint).strip()
            if api_endpoint and str(api_endpoint).strip()
            else "https://api.openai.com/v1/chat/completions"
        )

        # Try to determine the programming language of the current view.
        # This is injected into the system prompt via the %language% placeholder
        # so the LLM can provide context-aware code.
        syntax = self.view.syntax()
        language = syntax.name if syntax else "Plain Text"

        # Special case: Rust Enhanced package reports "Rust Enhanced" but we want just "Rust"
        if language == "Rust Enhanced":
            language = "Rust"

        # Dynamically inject the detected language into the system prompt.
        if system_prompt:
            system_prompt = system_prompt.replace("%language%", language)

        view_id = self.view.id()
        with active_tasks_lock:
            if view_id not in active_tasks:
                active_tasks[view_id] = {}

        selections = list(self.view.sel())
        view = self.view
        window = view.window()
        if not window:
            return

        def on_done(instruction):
            instruction = instruction.strip()
            if not instruction:
                return

            # Support Multi-Cursor:
            # We start an independent request for *each* selection block.
            # This allows users to generate/refactor multiple places in a file simultaneously.
            for sel in selections:
                task_id = str(uuid.uuid4())
                phantom_set = sublime.PhantomSet(view, f"goodvibes_{task_id}")

                # Register the task in the global dictionary
                with active_tasks_lock:
                    active_tasks[view_id][task_id] = {
                        "cancel": False,
                        "status": "thinking",
                        "view": view,
                        "phantom_set": phantom_set,
                    }

                if sel.empty():
                    # No selection; user wants to generate code at this line.
                    # Send only the instruction as the user message.
                    target_region = view.line(sel)
                    text = instruction
                else:
                    # Selection exists; user wants to transform the selected code.
                    # Send both the instruction and the code for context.
                    target_region = sel
                    selection_text = view.substr(target_region)
                    text = f"{instruction}\n\n{selection_text}"

                # If the target is totally empty, abort this specific task.
                if not text.strip():
                    with active_tasks_lock:
                        del active_tasks[view_id][task_id]
                    continue

                # Create a unique region key to track the text boundary in the editor.
                region_key = f"goodvibes_target_{task_id}"

                # We use `add_regions` with `sublime.HIDDEN` to track where the text is.
                # Why: If the user edits other parts of the file while the LLM is thinking,
                # Sublime Text will automatically adjust this region's coordinates so our
                # insertions don't end up in the wrong place.
                view.add_regions(
                    region_key, [target_region], "text", "", sublime.HIDDEN
                )

                # Offload the blocking network request to a background daemon thread
                # so the Sublime Text UI doesn't freeze.
                thread = threading.Thread(
                    target=self.run_llm,
                    args=(
                        api_endpoint,
                        api_key,
                        model_id,
                        system_prompt,
                        text,
                        view,
                        region_key,
                        task_id,
                        api_parameters,
                    ),
                )
                thread.daemon = True
                thread.start()

            update_phantoms(view)

        window.show_input_panel("✦ Instructions:", "", on_done, None, None)

    def run_llm(
        self,
        api_endpoint,
        api_key,
        model_id,
        system_prompt,
        text,
        view,
        region_key,
        task_id,
        api_parameters,
    ):
        """
        Background thread function that makes the HTTP request, parses the Server-Sent Events (SSE) stream,
        and batches the text chunks back to the main UI thread.
        """
        view_id = view.id()
        response = None
        try:
            # Construct the core LLM payload
            data = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                "stream": True,  # Streaming must be True for our chunk-by-chunk UX
            }

            # Inject any extra parameters defined by the user (like reasoning_effort).
            # We guard against overwriting the core keys we already set above, so a
            # misconfigured api_parameters entry can't silently break streaming or
            # swap the model mid-request.
            if isinstance(api_parameters, dict):
                reserved = {"model", "messages", "stream"}
                for key, value in api_parameters.items():
                    if key not in reserved:
                        data[key] = value

            # We use Python's built-in urllib to avoid forcing users to install third-party packages.
            # We explicitly add a User-Agent so services like Cloudflare don't block us as a bot.
            req = urllib.request.Request(
                api_endpoint,
                data=json.dumps(data).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Goodvibes/1.0 (Sublime Text Plugin)",
                },
                method="POST",
            )

            # Blocks here until the server accepts the connection and returns headers.
            # The timeout guards both the initial connection and individual reads, so
            # a stalled server won't leave the background thread stuck indefinitely.
            response = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)

            buffer = ""
            is_first_chunk = True

            def flush_buffer():
                """
                Batches accumulated characters and safely dispatches them to Sublime Text's
                main thread to edit the buffer.
                """
                nonlocal is_first_chunk, buffer
                if not buffer:
                    return
                chunk_str = buffer
                buffer = ""

                # The very first chunk REPLACES the original text selection.
                # All subsequent chunks are INSERTED at the end of the tracking region.
                # This ensures the prompt text is overwritten seamlessly.
                if is_first_chunk:
                    # Strip leading newlines from the very first chunk.
                    # Some models/endpoints erroneously emit whitespace before their actual
                    # response, causing unsightly gaps at the top of the generated output.
                    # We only strip here, not on subsequent chunks, because later newlines
                    # (e.g., between paragraphs) are almost certainly intentional.
                    chunk_str = chunk_str.lstrip("\n")
                    if not chunk_str:
                        return

                    is_first_chunk = False

                    with active_tasks_lock:
                        task = active_tasks.get(view_id, {}).get(task_id)
                        if task is not None:
                            # Update the task status so the phantom changes from "Thinking" to "Generating"
                            task["status"] = "generating"

                    # sublime.set_timeout executes the command safely on the main UI thread
                    sublime.set_timeout(
                        lambda: view.run_command(
                            "goodvibes_replace_text",
                            {"region_key": region_key, "text": chunk_str},
                        ),
                        0,
                    )
                else:
                    sublime.set_timeout(
                        lambda: view.run_command(
                            "goodvibes_insert_text",
                            {"region_key": region_key, "text": chunk_str},
                        ),
                        0,
                    )

            last_flush_time = 0

            # Stream processing loop
            # We use `readline()` to force reading chunk-by-chunk immediately, preventing
            # python from caching large blocks and causing the text to burst inconsistently.
            while True:
                # Check if the user hit Cmd+Z and cancelled this task mid-generation
                with active_tasks_lock:
                    cancelled = (
                        active_tasks.get(view_id, {})
                        .get(task_id, {})
                        .get("cancel", False)
                    )
                if cancelled:
                    break

                # If the user closed the tab, abort the stream and exit the loop immediately
                if not view.is_valid():
                    break

                line = response.readline()
                if not line:
                    break

                line = line.decode("utf-8").strip()

                # SSE lines must start with "data: "
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:]

                # "[DONE]" is the standard SSE termination signal from OpenAI-compatible APIs
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    # Extract the newly generated string characters
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")

                    if content:
                        # Append the characters to our buffer
                        buffer += content

                        # Optimization: We flush the buffer every FLUSH_INTERVAL seconds.
                        # Why: If we dispatched a `run_command` for every single 2-character token,
                        # Sublime Text's undo stack would get cluttered with hundreds of tiny operations,
                        # and the UI thread could lag. Batching provides smooth visuals while grouping edits.
                        current_time = time.time()
                        if current_time - last_flush_time > FLUSH_INTERVAL:
                            flush_buffer()
                            last_flush_time = current_time
                except json.JSONDecodeError:
                    pass

            # Flush any remaining text in the buffer after the stream ends
            flush_buffer()

        except urllib.error.HTTPError as e:
            # Handle API errors cleanly and display the server's JSON reason
            try:
                error_body = e.read().decode("utf-8")
            except (OSError, ValueError):
                error_body = ""
            err_msg = f"Goodvibes Error: HTTP {e.code} {e.reason}\n{error_body}"
            # Ensure evaluation of variables happens before the lambda executes
            sublime.set_timeout(lambda msg=err_msg: sublime.error_message(msg), 0)
        except (OSError, ValueError, RuntimeError) as e:
            err_msg = f"Goodvibes Error: {e!s}"
            sublime.set_timeout(lambda msg=err_msg: sublime.error_message(msg), 0)
        finally:
            # Ensure the socket connection is gracefully closed
            if response:
                with contextlib.suppress(OSError):
                    response.close()

            # De-register the task and cleanup phantoms regardless of success/error/cancel
            sublime.set_timeout(lambda: self.cleanup_task(view, region_key, task_id), 0)

    def cleanup_task(self, view, region_key, task_id):
        """Removes the task from state, erases tracking regions, and clears phantoms."""
        view_id = view.id()

        # We no longer need to track the buffer coordinates for this task
        # Only attempt to erase regions if the view still exists
        if view.is_valid():
            view.erase_regions(region_key)

        with active_tasks_lock:
            tasks = active_tasks.get(view_id)
            if tasks and task_id in tasks:
                # Erase the visual phantom ghost text
                # Only attempt to clear the phantoms if the view still exists
                if view.is_valid():
                    phantom_set = tasks[task_id].get("phantom_set")
                    if phantom_set:
                        phantom_set.update([])

                # Remove the task from the tracking dictionary
                del tasks[task_id]
                if not tasks:
                    del active_tasks[view_id]

        # Only attempt to re-trigger the animation loop if the view still exists
        if view.is_valid():
            update_phantoms(view)


class GoodvibesReplaceTextCommand(sublime_plugin.TextCommand):
    """
    Executed on the main UI thread. Replaces the target region with text.
    Used for the very first chunk of an LLM response.
    """

    def run(self, edit, region_key, text):
        regions = self.view.get_regions(region_key)
        if not regions:
            return

        region = regions[0]
        self.view.replace(edit, region, text)

        # Re-register the tracking region to encompass the newly replaced text.
        # This keeps the coordinates accurate for subsequent chunk insertions.
        new_region = sublime.Region(region.begin(), region.begin() + len(text))
        self.view.add_regions(region_key, [new_region], "text", "", sublime.HIDDEN)


class GoodvibesInsertTextCommand(sublime_plugin.TextCommand):
    """
    Executed on the main UI thread. Appends text to the end of the target region.
    Used for all subsequent chunks of an LLM response.
    """

    def run(self, edit, region_key, text):
        regions = self.view.get_regions(region_key)
        if not regions:
            return

        region = regions[0]
        insert_pt = region.end()
        self.view.insert(edit, insert_pt, text)

        # Expand the tracking region to encompass the new text
        new_region = sublime.Region(region.begin(), insert_pt + len(text))
        self.view.add_regions(region_key, [new_region], "text", "", sublime.HIDDEN)


class GoodvibesNoopCommand(sublime_plugin.TextCommand):
    """
    A dummy command used purely to suppress Sublime Text's default behavior.
    Returned by the UndoInterceptor to prevent a normal Undo when we are canceling tasks.
    """

    def run(self, edit):
        pass


class GoodvibesBulkUndoCommand(sublime_plugin.WindowCommand):
    """
    Automatically rolls back a sequence of Goodvibes edits.
    Why: Because streamed LLM text arrives in chunks, Sublime Text sees them as multiple
    distinct commands. To let the user undo a whole AI block instantly, we loop and undo
    every adjacent chunk.
    """

    def run(self):
        global is_bulk_undoing
        view = self.window.active_view()
        if not view:
            return

        # Set flag to prevent GoodvibesUndoInterceptor from interfering with our loop
        is_bulk_undoing = True
        try:
            # Look at the history at offset 0 (the last performed command)
            cmd, _, _ = view.command_history(0, True)

            # As long as the last command was one of our insertions, undo it.
            while cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                view.run_command("undo")
                cmd, _, _ = view.command_history(0, True)
        finally:
            is_bulk_undoing = False


class GoodvibesBulkRedoCommand(sublime_plugin.WindowCommand):
    """
    The exact opposite of BulkUndo. Fast-forwards through a sequence of undone Goodvibes edits.
    """

    def run(self):
        global is_bulk_undoing
        view = self.window.active_view()
        if not view:
            return

        is_bulk_undoing = True
        try:
            # Look at the history at offset 1 (the next command to be redone)
            cmd, _, _ = view.command_history(1, True)
            while cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                view.run_command("redo")
                cmd, _, _ = view.command_history(1, True)
        finally:
            is_bulk_undoing = False


class GoodvibesUndoInterceptor(sublime_plugin.EventListener):
    """
    Monitors user commands (like Cmd+Z / Cmd+Y) globally to hijack Undo/Redo behavior.
    """

    def on_text_command(self, view, command_name, args):
        if command_name == "undo":
            if is_bulk_undoing:
                return None

            view_id = view.id()

            # SCENARIO 1: The user hits Undo while the LLM is currently generating text.
            # We cancel all active streams. The `has_tasks` check is kept inside the
            # lock so structural mutations from other threads can't empty the dict
            # between the check and the return.
            with active_tasks_lock:
                tasks = active_tasks.get(view_id)
                has_tasks = bool(tasks)
                if has_tasks:
                    for tid in list(tasks):
                        tasks[tid]["cancel"] = True

            if has_tasks:
                # We substitute their Undo with our NoopCommand.
                # Why: This swallows the keypress, stopping generation without
                # reverting any text on the screen. It allows users to "stop" the AI
                # and keep partial output.
                return ("goodvibes_noop", None)

            # SCENARIO 2: No active streams. The user hits Undo, and the last thing
            # they did was generate text via Goodvibes.
            cmd, _, _ = view.command_history(0, True)
            if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                # We schedule a bulk undo to remove the entire AI block at once.
                # We use set_timeout so this WindowCommand can be fired outside
                # the scope of the current TextCommand phase. The view may have
                # been closed by the time this fires, so we guard against
                # view.window() returning None.
                sublime.set_timeout(
                    lambda: (
                        view.window()
                        and view.window().run_command("goodvibes_bulk_undo")
                    ),
                    0,
                )
                return ("goodvibes_noop", None)

        elif command_name in ("redo", "redo_or_repeat"):
            if is_bulk_undoing:
                return None

            # SCENARIO 3: The user hits Redo, and the next thing in the stack
            # is an undone Goodvibes text generation block. We bulk redo it.
            cmd, _, _ = view.command_history(1, True)
            if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                sublime.set_timeout(
                    lambda: (
                        view.window()
                        and view.window().run_command("goodvibes_bulk_redo")
                    ),
                    0,
                )
                return ("goodvibes_noop", None)

        # Allow any other command to proceed normally
        return None
