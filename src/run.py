import sys
import os
import signal
import asyncio
from enum import Enum, auto

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign, ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.filters import HasFocus, Condition

# Import the DownloadManager from the other file
try:
    from download_manager import DownloadManager, DownloadItem
except ImportError:
    print("Error: Could not find download_manager.py.")
    print("Please ensure it's in the same directory as run.py.")
    sys.exit(1)

# --- Global Variables & State ---
manager = DownloadManager()
last_exception = None # To display errors briefly

# Enum to track the current input mode
class InputMode(Enum):
    COMMAND = auto() # Waiting for a command key (a, p, r, l, q)
    ENTERING_URL = auto()
    ENTERING_PAUSE_FILENAME = auto()
    ENTERING_RESUME_FILENAME = auto()

current_input_mode = InputMode.COMMAND
status_message = "Keys: [a]dd [p]ause [r]esume [l]ist [q]uit | Esc to cancel input"
prompt_message = "" # Message shown above input field

# --- UI Content Functions ---

def get_download_list_content():
    """ Returns FormattedTextList for the download status window. """
    global last_exception
    try:
        status_str = manager.get_status()
        if last_exception:
           status_str += f"\n\nError processing input: {last_exception}"
           last_exception = None
        return ANSI(status_str)
    except Exception as e:
        last_exception = e
        return ANSI(f"Error retrieving status: {e}")

def get_prompt_message_content():
    """ Returns text for the prompt line above the input field. """
    return ANSI(prompt_message)

def get_status_bar_content():
    """ Returns FormattedTextList for the bottom status bar. """
    return ANSI(status_message)

# --- Input Buffer ---
# This buffer will be used when prompting for URL or filename
input_buffer = Buffer()

# --- Key Bindings ---
bindings = KeyBindings()

def reset_input_state():
    """ Reset state after command execution or cancellation. """
    global current_input_mode, prompt_message
    input_buffer.reset()
    current_input_mode = InputMode.COMMAND
    prompt_message = ""
    # Ensure focus leaves the input buffer if it's not needed
    # We might need to explicitly set focus back to a non-input area if desired
    # For now, focus stays where it is, but the input field might hide.


# --- Global Command Keys (available when InputMode is COMMAND) ---

@bindings.add('a', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'a' pressed: Prepare to enter URL. """
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_URL
    prompt_message = "Enter URL to add:"
    status_message = "Type URL and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window) # Focus the input field

@bindings.add('p', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'p' pressed: Prepare to enter filename to pause. """
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_PAUSE_FILENAME
    prompt_message = "Enter filename to PAUSE:"
    status_message = "Type filename and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('r', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'r' pressed: Prepare to enter filename to resume. """
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_RESUME_FILENAME
    prompt_message = "Enter filename to RESUME:"
    status_message = "Type filename and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('l', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'l' pressed: Refresh status message (list updates automatically). """
    global status_message
    status_message = "Download list updated. Keys: [a]dd [p]ause [r]esume [l]ist [q]uit"
    # No need to change mode or focus

@bindings.add('q', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
@bindings.add('c-c') # Keep Ctrl+C as global exit
@bindings.add('c-q') # Keep Ctrl+Q as global exit
def _(event):
    """ 'q', Ctrl+C, Ctrl+Q pressed: Exit application. """
    print("\nExit requested. Stopping manager...")
    global status_message
    status_message = "Exiting..."
    manager.stop(graceful=True)
    # Schedule exit via the event loop
    loop = asyncio.get_event_loop()
    # Use call_soon_threadsafe if stopping from a different thread context
    # If called directly from prompt_toolkit event, call_soon should work
    loop.call_soon(lambda: asyncio.ensure_future(app.cancel()))


# --- Input Field Keys (available when focused and not in COMMAND mode) ---

@bindings.add('enter', filter=HasFocus(input_buffer) & Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    """ Enter pressed in input field: process the entered text. """
    global status_message, last_exception
    entered_text = input_buffer.text.strip()
    last_exception = None # Clear previous error

    try:
        if current_input_mode == InputMode.ENTERING_URL:
            if entered_text:
                manager.add_download(entered_text)
                status_message = f"Added '{entered_text[:50]}{'...' if len(entered_text)>50 else ''}'."
            else:
                status_message = "Add cancelled (no URL entered)."

        elif current_input_mode == InputMode.ENTERING_PAUSE_FILENAME:
            if entered_text:
                if manager.pause_download(entered_text):
                    status_message = f"Attempting to pause '{entered_text}'..."
                else:
                     # pause_download prints details, set a general message here
                     item = manager.downloads.get(entered_text)
                     if item: status_message = f"Could not pause '{entered_text}' (status: {item.status})."
                     else: status_message = f"Download '{entered_text}' not found for pausing."
            else:
                status_message = "Pause cancelled (no filename entered)."

        elif current_input_mode == InputMode.ENTERING_RESUME_FILENAME:
            if entered_text:
                if manager.resume_download(entered_text):
                    status_message = f"Attempting to resume '{entered_text}'..."
                else:
                     # resume_download prints details
                     item = manager.downloads.get(entered_text)
                     if item: status_message = f"Could not resume '{entered_text}' (status: {item.status})."
                     else: status_message = f"Download '{entered_text}' not found for resuming."
            else:
                status_message = "Resume cancelled (no filename entered)."

    except Exception as e:
        print(f"\nError processing input '{entered_text}': {e}")
        status_message = f"Error: {e}"
        last_exception = e

    reset_input_state()
    # Optionally focus back to the main area after command execution
    # event.app.layout.focus(download_list_window) # Example


@bindings.add('escape', filter=HasFocus(input_buffer) | Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    """ Escape pressed: Cancel current input mode. """
    global status_message
    status_message = "Input cancelled. Keys: [a]dd [p]ause [r]esume [l]ist [q]uit"
    reset_input_state()
    # Optionally focus back to the main area
    # event.app.layout.focus(download_list_window) # Example


# --- Layout Definition ---

# Window to display the download list
download_list_window = Window(
    content=FormattedTextControl(text=get_download_list_content, focusable=True), # Make focusable
    wrap_lines=True
)

# Window for the prompt message (only visible when needed)
prompt_message_window = Window(
    content=FormattedTextControl(text=get_prompt_message_content),
    height=1,
    style="class:prompt-message",
    align=WindowAlign.LEFT
)

# Window for the user input (URL or filename)
input_window = Window(
    content=BufferControl(buffer=input_buffer, focusable=True), # Make focusable
    height=1,
    style="class:input-field"
)

# Window for the status bar at the bottom
status_bar_window = Window(
    content=FormattedTextControl(text=get_status_bar_content),
    height=1,
    style="class:status-bar",
    align=WindowAlign.LEFT
)

# Conditional container for the prompt + input field
# Only show these when not in COMMAND mode
input_area = ConditionalContainer(
    content=HSplit([
        prompt_message_window,
        input_window,
    ]),
    filter=Condition(lambda: current_input_mode != InputMode.COMMAND)
)

# Main layout
root_container = HSplit([
    download_list_window,       # Main content area
    Window(height=1, char='-', style='class:separator'), # Separator line
    input_area,                 # Prompt and Input (conditional)
    status_bar_window           # Status message line (always visible)
])

# Create the layout - focus the main list initially
layout = Layout(root_container, focused_element=download_list_window)


# --- Application Setup ---
app = Application(
    layout=layout,
    key_bindings=bindings,
    full_screen=True,
    mouse_support=True,
    refresh_interval=0.5 # Refresh UI for status updates
)

# --- Signal Handling & Main Execution (mostly unchanged) ---
def handle_sigterm(signum, frame):
    """ Handle SIGTERM signal for graceful shutdown. """
    print("\nSIGTERM received. Stopping manager and exiting...")
    global status_message
    status_message = "SIGTERM received, exiting..." # Update UI briefly if possible
    manager.stop(graceful=True)
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(app.cancel()))
    except RuntimeError:
        sys.exit(1)

async def main():
    """ Asynchronous main function to run the application. """
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm, signal.SIGTERM, None)

    print("Starting download manager worker...")
    manager.start()

    print("Launching TUI...")
    await app.run_async()

    print("TUI exited. Final shutdown.")
    if manager.worker_thread and manager.worker_thread.is_alive():
         manager.stop(graceful=True) # Ensure stop on exit


if __name__ == "__main__":
    if sys.platform == "win32":
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt caught in main. Exiting.")
        manager.stop(graceful=False)
    finally:
        print("Application finished.")
