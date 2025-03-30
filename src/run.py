import sys
import os
import signal
import asyncio
from enum import Enum, auto
from typing import List, Tuple

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign, ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
# Use FormattedText for constructing styled text directly
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import HasFocus, Condition
from prompt_toolkit.styles import Style

# Assume download_manager exists and import directly
from download_manager import DownloadManager, DownloadItem

# --- Constants ---
PROGRESS_BAR_WIDTH = 25 # Width of the text progress bar in characters

# --- Global Variables & State ---
manager = DownloadManager()
last_exception = None

class InputMode(Enum):
    COMMAND = auto()
    ENTERING_URL = auto()
    ENTERING_PAUSE_FILENAME = auto()
    ENTERING_RESUME_FILENAME = auto()

current_input_mode = InputMode.COMMAND
status_message = "Keys: [a]dd [p]ause [r]esume [P]auseAll [R]esumeAll [q]uit | Esc: cancel"
prompt_message = ""

# --- UI Styling ---
ui_style = Style.from_dict({
    'status-queued': 'fg:gray',
    'status-downloading': 'fg:cyan', # Style for the text part of downloading item
    'status-paused': 'fg:orange',
    'status-completed': 'fg:ansigreen', # Use ansigreen for better visibility maybe
    'status-error': 'fg:ansired bold',
    # Styles for the manual progress bar components
    'progress-bar': 'bg:#666666', # Background of the empty part
    'progress-bar-filled': 'bg:ansicyan fg:ansiblack', # Background of the filled part
    'filename': 'bold',
    'percentage': 'fg:cyan',
    'size': 'fg:gray',
    'prompt-message': 'fg:yellow',
    'status-bar': 'bg:#222222 fg:white',
    'separator': 'fg:#666666',
    'error-message': 'fg:ansired',
})

# --- UI Content Functions ---

def format_size(byte_size: int) -> str:
    if byte_size < 1024: return f"{byte_size} B"
    if byte_size < 1024**2: return f"{byte_size/1024:.1f} KB"
    if byte_size < 1024**3: return f"{byte_size/(1024**2):.2f} MB"
    return f"{byte_size/(1024**3):.2f} GB"

def create_manual_progress_bar(item: DownloadItem) -> List[Tuple[str, str]]:
    """ Creates a list of (style, text) tuples for a manual progress bar line. """
    percentage = 0.0
    if item.total_size > 0:
        percentage = min(100.0, (item.downloaded_size / item.total_size) * 100)

    filled_width = int(PROGRESS_BAR_WIDTH * percentage / 100)
    # Ensure empty width doesn't go negative if somehow percentage > 100
    empty_width = max(0, PROGRESS_BAR_WIDTH - filled_width)

    size_str = f"{format_size(item.downloaded_size)}/{format_size(item.total_size)}" if item.total_size > 0 else f"{format_size(item.downloaded_size)}"
    percent_str = f"{percentage:.1f}%"

    # Build the list of (style, text) tuples for this line
    line_tuples = [
        ('class:filename', f" {item.filename:<20} "), # Pad filename for alignment
        ('', '['),
        ('class:progress-bar-filled', '━' * filled_width), # Use line char
        ('class:progress-bar', ' ' * empty_width),        # Use space for empty
        ('', '] '),
        ('class:percentage', f"{percent_str:>6} "),     # Pad percentage
        ('class:size', f"({size_str})"),
        ('', '\n') # Add newline at the end of the line
    ]
    return line_tuples

def get_download_list_content() -> FormattedText:
    """ Returns FormattedText (a list of tuples) for the download status window. """
    global last_exception
    # The final list of (style, text) tuples to be rendered
    all_lines: List[Tuple[str, str]] = []
    try:
        items = sorted(manager.get_items(), key=lambda x: x.filename)

        if not items:
             all_lines.append(('', "No downloads yet. Press 'a' to add a URL.\n"))

        for item in items:
            if item.status == 'downloading':
                # create_manual_progress_bar returns a list of tuples for one line
                progress_line_tuples = create_manual_progress_bar(item)
                all_lines.extend(progress_line_tuples) # Add these tuples to the main list
            else:
                # For other statuses, just add the basic info line
                style_class = f"class:status-{item.status}"
                base_text = str(item) # Uses DownloadItem.__str__
                # Add padding to align with progress bar roughly
                all_lines.append((style_class, f" {base_text:<50}\n"))

        if last_exception:
           all_lines.append(('', "\n"))
           all_lines.append(('class:error-message', f"Error: {last_exception}\n"))
           last_exception = None

        # The FormattedTextControl expects a list of (style_str, text) tuples.
        # 'all_lines' is now exactly that structure.
        return all_lines

    except Exception as e:
        print(f"ERROR in get_download_list_content: {e}") # Print error for debugging
        import traceback
        traceback.print_exc() # Print stack trace
        last_exception = e
        return [('class:error-message', f"Error retrieving status: {e}\n")]


def get_prompt_message_content() -> FormattedText:
     # Ensure return type is list of tuples
    return [('class:prompt-message', prompt_message)]

def get_status_bar_content() -> FormattedText:
     # Ensure return type is list of tuples
    return [('class:status-bar', f" {status_message} ")]

# --- Input Buffer ---
input_buffer = Buffer()

# --- Key Bindings (Unchanged from previous working version) ---
bindings = KeyBindings()

def reset_input_state(app_layout):
    global current_input_mode, prompt_message, status_message
    input_buffer.reset()
    current_input_mode = InputMode.COMMAND
    prompt_message = ""
    status_message = "Keys: [a]dd [p]ause [r]esume [P]auseAll [R]esumeAll [q]uit | Esc: cancel"
    app_layout.focus(download_list_window)

@bindings.add('a', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_URL
    prompt_message = "Enter URL to add:"
    status_message = "Type URL and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('p', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_PAUSE_FILENAME
    prompt_message = "Enter filename to PAUSE:"
    status_message = "Type filename and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('r', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_RESUME_FILENAME
    prompt_message = "Enter filename to RESUME:"
    status_message = "Type filename and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('P', filter=Condition(lambda: current_input_mode == InputMode.COMMAND)) # Shift+P
def _(event):
    global status_message
    if manager.pause_all(): status_message = "Signalled all active downloads to pause."
    else: status_message = "No active downloads to pause."

@bindings.add('R', filter=Condition(lambda: current_input_mode == InputMode.COMMAND)) # Shift+R
def _(event):
    global status_message
    if manager.resume_all(): status_message = "Signalled all paused downloads to resume."
    else: status_message = "No paused downloads to resume."

@bindings.add('q', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
@bindings.add('c-c')
@bindings.add('c-q')
def _(event):
    print("\nExit requested. Stopping manager...")
    global status_message
    status_message = "Exiting..."
    manager.stop(graceful=True)
    event.app.exit() # Correct way to exit app

@bindings.add('enter', filter=HasFocus(input_buffer) & Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    global status_message, last_exception
    entered_text = input_buffer.text.strip()
    last_exception = None
    try:
        if current_input_mode == InputMode.ENTERING_URL:
            if entered_text:
                if manager.add_download(entered_text): status_message = f"Added."
                else: status_message = f"Failed to add (duplicate?)."
            else: status_message = "Add cancelled."
        elif current_input_mode == InputMode.ENTERING_PAUSE_FILENAME:
            if entered_text:
                if manager.pause_download(entered_text): status_message = f"Attempting pause..."
                else: status_message = f"Could not pause."
            else: status_message = "Pause cancelled."
        elif current_input_mode == InputMode.ENTERING_RESUME_FILENAME:
            if entered_text:
                if manager.resume_download(entered_text): status_message = f"Attempting resume..."
                else: status_message = f"Could not resume."
            else: status_message = "Resume cancelled."
    except Exception as e:
        print(f"\nError processing input '{entered_text}': {e}")
        status_message = f"Error: {e}"
        last_exception = e
    reset_input_state(event.app.layout)

@bindings.add('escape', filter=Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    global status_message
    status_message = "Input cancelled."
    reset_input_state(event.app.layout)


# --- Layout Definition ---

# Use the callable `get_download_list_content` which returns List[Tuple[str, str]]
# This list is directly understood by FormattedTextControl
download_list_window = Window(
    content=FormattedTextControl(text=get_download_list_content, focusable=True),
    wrap_lines=False # Keep wrap lines False
)

prompt_message_window = Window(
    content=FormattedTextControl(text=get_prompt_message_content),
    height=1,
    # style="class:prompt-message", # Style applied via FormattedText tuple
    align=WindowAlign.LEFT
)

input_window = Window(
    content=BufferControl(buffer=input_buffer, focusable=True),
    height=1,
    # style="class:input-field" # Can style if needed
)

status_bar_window = Window(
    content=FormattedTextControl(text=get_status_bar_content),
    height=1,
    align=WindowAlign.LEFT
)

input_area = ConditionalContainer(
    content=HSplit([
        prompt_message_window,
        input_window,
    ]),
    filter=Condition(lambda: current_input_mode != InputMode.COMMAND)
)

root_container = HSplit(children=[
    download_list_window,
    input_area,
    status_bar_window
    ], padding_char='-', padding=1, padding_style='class:separator')

layout = Layout(root_container, focused_element=download_list_window)

# --- Application Setup ---
app = Application(
    layout=layout,
    key_bindings=bindings,
    style=ui_style, # Apply the custom style
    full_screen=True,
    mouse_support=True,
    refresh_interval=0.5
)

# --- Signal Handling & Main Execution (Unchanged) ---
def handle_sigterm(signum, frame):
    print("\nSIGTERM received. Stopping manager and exiting...")
    global status_message
    status_message = "SIGTERM received, exiting..."
    manager.stop(graceful=True)
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(app.exit)
    except RuntimeError:
         sys.exit(1)

async def main():
    loop = asyncio.get_running_loop()
    if hasattr(signal, "SIGTERM"):
        loop.add_signal_handler(signal.SIGTERM, handle_sigterm, signal.SIGTERM, None)

    print("Starting download manager worker...")
    manager.start()

    print("Launching TUI...")
    await app.run_async()

    print("TUI exited. Final shutdown procedures...")
    if manager.worker_thread and manager.worker_thread.is_alive():
         print("Ensuring download manager worker is stopped...")
         manager.stop(graceful=False)


if __name__ == "__main__":
    if sys.platform == "win32":
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt caught in main. Forcing exit.")
        if manager: manager.stop(graceful=False)
    finally:
        print("Application finished.")
