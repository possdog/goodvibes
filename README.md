# Goodvibes

> 🤖 This project was made by an LLM with a human in the loop.

A powerful, dependency-free Sublime Text 4 package that brings inline LLM assistance directly into your editor.

With Goodvibes, you can highlight any text (or just place your cursor on a line) and invoke an OpenAI-compatible API to seamlessly refactor, rewrite, or generate code. The LLM's response streams directly into your buffer in real-time, matching your existing workflow.

## Features

- **Real-Time Streaming**: Watch the AI type out its response right before your eyes.
- **Smart Undo & Redo**: Don't like the AI's suggestion? Press `⌘+Z` (`Ctrl+Z`) mid-generation to stop it instantly while keeping whatever it already wrote. Pressing `⌘+Z` after it finishes will undo the *entire* generated block in one go. You can even redo the entire block with a single `⌘+Shift+Z`!
- **Multi-Cursor Support**: Invoke the AI on multiple selections simultaneously. Goodvibes will process each selection independently and stream the results in parallel.
- **Sleek UX**: No clunky status bar shifting. Goodvibes uses Sublime Text's inline phantoms to render a beautiful, non-disruptive "ghost" text animation (e.g., `✦ Thinking...`) at the exact point of insertion.
- **Bring Your Own API**: Works with any OpenAI-compatible endpoint (OpenAI, Anthropic via proxies, local models via LM Studio or Ollama).
- **Dependency-Free**: Runs entirely on Python's standard library. No `pip install` or Package Control dependency management required.

## Installation

*Note: Goodvibes is not currently available on Package Control.*

1. Download the latest `Goodvibes.sublime-package` from the releases page (or build it yourself using `./bundle.sh`).
2. Open Sublime Text.
3. Go to **Preferences > Browse Packages...**.
4. Navigate up one directory level to the `Installed Packages` folder.
5. Drop `Goodvibes.sublime-package` into the `Installed Packages` folder.
6. Restart Sublime Text (optional, but recommended).

## Configuration

Before you can use Goodvibes, you need to configure your API provider.

1. Open the Command Palette (`⌘+Shift+P` on macOS, `Ctrl+Shift+P` on Windows/Linux).
2. Search for **Preferences: Goodvibes Settings**.

This will open a dual-pane layout. In the right pane (your User settings), configure the following variables:

```json
{
    // The base URL of your OpenAI-compatible API.
    "api_endpoint": "https://openrouter.ai/api/v1/chat/completions",

    // Your API key for the chosen provider.
    "api_key": "sk-...",

    // The specific model you want to use.
    "model_id": "moonshotai/kimi-k2.5",

    // Optional: Extra JSON parameters to inject into the API request payload.
    // By default, this disables thinking/reasoning for modern models to ensure low Time To First Token latency.
    "api_parameters": {
        "reasoning_effort": "none"
    },

    // Optional: The prompt that instructs the model on how to behave.
    // The "%language%" variable is automatically replaced with the file's current syntax (e.g. "Python").
    "system_prompt": "You are a helpful coding assistant. The user is currently editing a %language% file..."
}
```

## Usage

1. Select the text you want to modify (e.g., a comment describing a function, or a block of code to refactor). If you don't select anything, Goodvibes will simply use the line where your cursor is currently placed.
2. Invoke Goodvibes using one of the following methods:
   - **Command Palette**: Search for `Goodvibes: Invoke AI on Selection`.
   - **Context Menu**: Right-click the selection and choose `Invoke AI on Selection`.
   - **Keyboard Shortcut**: By default, no shortcut is bound. You can easily add one by searching for `Preferences: Goodvibes Key Bindings` in the Command Palette. For example:
     ```json
     [
         {
             "keys": ["ctrl+shift+g"],
             "command": "goodvibes_invoke_ai"
         }
     ]
     ```

The AI's response will replace the selection (or the line) and stream inline!

## Troubleshooting

- If nothing happens, check the Sublime Text Console (`View > Show Console`) for any Python errors.
- Ensure your API key is valid and the endpoint URL is correct (you must include `/chat/completions` for most compatible APIs).
