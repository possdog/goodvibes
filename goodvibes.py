import sublime
import sublime_plugin
import threading
import time
import uuid
import urllib.request
import urllib.error
import json

# Global state to track active tasks per view
# Key: view.id(), Value: dict of task_id -> {"cancel": False, "status": "thinking", "view": view}
active_tasks = {}
is_bulk_undoing = False
is_animating = False


def get_settings():
    return sublime.load_settings("goodvibes.sublime-settings")


def animate_phantoms():
    global is_animating
    if not active_tasks:
        is_animating = False
        return

    tick = int(time.time() * 2) % 2
    icon = "✦" if tick == 0 else "✧"

    for view_id, tasks in list(active_tasks.items()):
        for task_id, task in list(tasks.items()):
            view = task.get("view")
            if not view or not view.is_valid():
                continue

            phantom_set = task.get("phantom_set")
            if not phantom_set:
                continue

            if task.get("cancel"):
                phantom_set.update([])
                continue

            region_key = f"goodvibes_target_{task_id}"
            regions = view.get_regions(region_key)
            if not regions:
                continue

            region = regions[0]
            # Place phantom at the end of the region
            phantom_region = sublime.Region(region.end(), region.end())

            is_thinking = task.get("status") == "thinking"
            state_text = "Thinking" if is_thinking else "Generating"

            content = f'<body id="goodvibes"><span style="color: color(var(--foreground) alpha(0.5)); font-style: italic; padding-left: 0.5rem;">{icon} {state_text}...</span></body>'

            phantom = sublime.Phantom(phantom_region, content, sublime.LAYOUT_INLINE)
            phantom_set.update([phantom])

    if active_tasks:
        sublime.set_timeout(animate_phantoms, 500)
    else:
        is_animating = False


def update_phantoms(view):
    global is_animating
    if active_tasks and not is_animating:
        is_animating = True
        animate_phantoms()


class GoodvibesInvokeAiCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        settings = get_settings()
        api_key = settings.get("api_key")
        api_endpoint = settings.get("api_endpoint")
        model_id = settings.get("model_id")
        system_prompt = settings.get("system_prompt")
        api_parameters = settings.get("api_parameters", {})

        if not api_key or not model_id:
            sublime.error_message("Goodvibes: Please set your api_key and model_id in the Goodvibes settings.")
            return

        api_endpoint = str(api_endpoint).strip() if api_endpoint and str(api_endpoint).strip() else "https://api.openai.com/v1/chat/completions"

        # Try to determine the language of the current view
        syntax = self.view.syntax()
        language = syntax.name if syntax else "Plain Text"

        # Replace %language% in the system prompt
        if system_prompt:
            system_prompt = system_prompt.replace("%language%", language)

        view_id = self.view.id()
        if view_id not in active_tasks:
            active_tasks[view_id] = {}

        selections = list(self.view.sel())

        # Start a request for each selection
        for sel in selections:
            task_id = str(uuid.uuid4())
            phantom_set = sublime.PhantomSet(self.view, f"goodvibes_{task_id}")
            active_tasks[view_id][task_id] = {"cancel": False, "status": "thinking", "view": self.view, "phantom_set": phantom_set}

            # If no selection, use the current line
            if sel.empty():
                target_region = self.view.line(sel)
            else:
                target_region = sel

            # Read text
            text = self.view.substr(target_region)
            if not text.strip():
                # Cleanup if the selection is completely empty
                del active_tasks[view_id][task_id]
                continue

            # Create a unique region key to track the text
            region_key = f"goodvibes_target_{task_id}"

            # We track the initial target region so it can be replaced when the first chunk arrives
            self.view.add_regions(region_key, [target_region], "text", "", sublime.HIDDEN)

            # Start thread
            thread = threading.Thread(
                target=self.run_llm,
                args=(api_endpoint, str(api_key).strip(), model_id, system_prompt, text, self.view, region_key, task_id, api_parameters)
            )
            thread.daemon = True
            thread.start()

        update_phantoms(self.view)

    def run_llm(self, api_endpoint, api_key, model_id, system_prompt, text, view, region_key, task_id, api_parameters):
        view_id = view.id()
        response = None
        try:
            data = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                "stream": True
            }
            if isinstance(api_parameters, dict):
                data.update(api_parameters)

            req = urllib.request.Request(
                api_endpoint,
                data=json.dumps(data).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Goodvibes/1.0 (Sublime Text Plugin)"
                },
                method="POST"
            )

            response = urllib.request.urlopen(req)

            buffer = []
            is_first_chunk = True

            def flush_buffer():
                nonlocal is_first_chunk
                if not buffer:
                    return
                chunk_str = "".join(buffer)
                buffer.clear()

                if is_first_chunk:
                    is_first_chunk = False
                    if view_id in active_tasks and task_id in active_tasks[view_id]:
                        active_tasks[view_id][task_id]["status"] = "generating"
                    sublime.set_timeout(lambda: view.run_command("goodvibes_replace_text", {"region_key": region_key, "text": chunk_str}), 0)
                else:
                    sublime.set_timeout(lambda: view.run_command("goodvibes_insert_text", {"region_key": region_key, "text": chunk_str}), 0)

            last_flush_time = 0

            while True:
                if active_tasks.get(view_id, {}).get(task_id, {}).get("cancel", False):
                    break

                line = response.readline()
                if not line:
                    break

                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        buffer.append(content)

                        current_time = time.time()
                        if current_time - last_flush_time > 0.1:
                            flush_buffer()
                            last_flush_time = current_time
                except json.JSONDecodeError:
                    pass

            # Final flush
            flush_buffer()

        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode('utf-8')
            except Exception:
                error_body = ""
            err_msg = f"Goodvibes HTTP Error: {e.code} {e.reason}\n{error_body}"
            sublime.set_timeout(lambda msg=err_msg: sublime.error_message(msg), 0)
        except Exception as e:
            err_msg = f"Goodvibes API Error:\n{str(e)}"
            sublime.set_timeout(lambda msg=err_msg: sublime.error_message(msg), 0)
        finally:
            if response:
                try:
                    response.close()
                except Exception:
                    pass
            # Cleanup
            sublime.set_timeout(lambda: self.cleanup_task(view, region_key, task_id), 0)

    def cleanup_task(self, view, region_key, task_id):
        view_id = view.id()
        view.erase_regions(region_key)

        if view_id in active_tasks and task_id in active_tasks[view_id]:
            phantom_set = active_tasks[view_id][task_id].get("phantom_set")
            if phantom_set:
                phantom_set.update([])
            del active_tasks[view_id][task_id]
            if not active_tasks[view_id]:
                del active_tasks[view_id]

        update_phantoms(view)


class GoodvibesReplaceTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, region_key, text):
        regions = self.view.get_regions(region_key)
        if not regions:
            return

        region = regions[0]
        self.view.replace(edit, region, text)

        # Update region to track new text precisely
        new_region = sublime.Region(region.begin(), region.begin() + len(text))
        self.view.add_regions(region_key, [new_region], "text", "", sublime.HIDDEN)


class GoodvibesInsertTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, region_key, text):
        regions = self.view.get_regions(region_key)
        if not regions:
            return

        region = regions[0]
        insert_pt = region.end()
        self.view.insert(edit, insert_pt, text)

        # Expand tracked region to encompass the new text
        new_region = sublime.Region(region.begin(), insert_pt + len(text))
        self.view.add_regions(region_key, [new_region], "text", "", sublime.HIDDEN)


class GoodvibesNoopCommand(sublime_plugin.TextCommand):
    """A dummy command used to suppress actual undo when intercepting Cmd+Z mid-generation."""

    def run(self, edit):
        pass


class GoodvibesBulkUndoCommand(sublime_plugin.WindowCommand):
    def run(self):
        global is_bulk_undoing
        view = self.window.active_view()
        if not view:
            return

        is_bulk_undoing = True
        try:
            view.run_command("undo")
            while True:
                cmd, _, _ = view.command_history(0, True)
                if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                    view.run_command("undo")
                else:
                    break
        finally:
            is_bulk_undoing = False


class GoodvibesBulkRedoCommand(sublime_plugin.WindowCommand):
    def run(self):
        global is_bulk_undoing
        view = self.window.active_view()
        if not view:
            return

        is_bulk_undoing = True
        try:
            view.run_command("redo")
            while True:
                cmd, _, _ = view.command_history(1, True)
                if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                    view.run_command("redo")
                else:
                    break
        finally:
            is_bulk_undoing = False


class GoodvibesUndoInterceptor(sublime_plugin.EventListener):
    def on_text_command(self, view, command_name, args):
        global is_bulk_undoing
        if command_name == "undo":
            if is_bulk_undoing:
                return None

            view_id = view.id()
            if view_id in active_tasks and active_tasks[view_id]:
                # We have active tasks. Cancel them.
                for task_id in active_tasks[view_id]:
                    active_tasks[view_id][task_id]["cancel"] = True
                    phantom_set = active_tasks[view_id][task_id].get("phantom_set")
                    if phantom_set:
                        phantom_set.update([])

                # Return 'goodvibes_noop' to suppress actual undo and just stop generation
                return ("goodvibes_noop", None)

            # If no active tasks, check if the user is undoing our AI insertions
            cmd, _, _ = view.command_history(0, True)
            if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                sublime.set_timeout(lambda: view.window().run_command("goodvibes_bulk_undo"), 0)
                return ("goodvibes_noop", None)

        elif command_name in ("redo", "redo_or_repeat"):
            if is_bulk_undoing:
                return None

            # Check if the next command to redo is our AI insertion
            cmd, _, _ = view.command_history(1, True)
            if cmd in ("goodvibes_insert_text", "goodvibes_replace_text"):
                sublime.set_timeout(lambda: view.window().run_command("goodvibes_bulk_redo"), 0)
                return ("goodvibes_noop", None)

        return None
