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
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import HasFocus, Condition
from prompt_toolkit.styles import Style
import logging # Use logging instead of print

# Assume download_manager exists and import directly
from download_manager import DownloadManager, DownloadItem

# --- Constants ---
PROGRESS_BAR_WIDTH = 25

# --- Global Variables & State ---
manager = DownloadManager()
last_exception = None

# Update InputMode Enum
class InputMode(Enum):
    COMMAND = auto()
    ENTERING_URL = auto()
    ENTERING_PAUSE_NUMBER = auto()  # Renamed
    ENTERING_RESUME_NUMBER = auto() # Renamed

current_input_mode = InputMode.COMMAND
# Update initial status message slightly
status_message = "Keys: [a]dd [p]ause# [r]esume# [P]auseAll [R]esumeAll [q]uit | Esc: cancel"
prompt_message = ""

# --- UI Styling (Unchanged) ---
ui_style = Style.from_dict({
    'status-queued': 'fg:gray',
    'status-downloading': 'fg:cyan',
    'status-paused': 'fg:orange',
    'status-completed': 'fg:ansigreen',
    'status-error': 'fg:ansired bold',
    'progress-bar': 'bg:#666666',
    'progress-bar-filled': 'bg:ansicyan fg:ansiblack',
    'filename': 'bold',
    'percentage': 'fg:cyan',
    'size': 'fg:gray',
    'item-index': 'fg:yellow', # Style for the number prefix
    'prompt-message': 'fg:yellow',
    'status-bar': 'bg:#222222 fg:white',
    'separator': 'fg:#666666',
    'error-message': 'fg:ansired',
})

# --- UI Content Functions ---

def format_size(byte_size: int) -> str:
    # (Unchanged)
    if byte_size < 1024: return f"{byte_size} B"
    if byte_size < 1024**2: return f"{byte_size/1024:.1f} KB"
    if byte_size < 1024**3: return f"{byte_size/(1024**2):.2f} MB"
    return f"{byte_size/(1024**3):.2f} GB"

def create_manual_progress_bar_tuples(index: int, item: DownloadItem) -> List[Tuple[str, str]]:
    """ Creates tuples for ONE line representing a progress bar, including index. """
    percentage = 0.0
    if item.total_size > 0:
        percentage = min(100.0, (item.downloaded_size / item.total_size) * 100)

    filled_width = int(PROGRESS_BAR_WIDTH * percentage / 100)
    empty_width = max(0, PROGRESS_BAR_WIDTH - filled_width)

    size_str = f"{format_size(item.downloaded_size)}/{format_size(item.total_size)}" if item.total_size > 0 else f"{format_size(item.downloaded_size)}"
    percent_str = f"{percentage:.1f}%"

    line_tuples = [
        # Add the index number with style
        ('class:item-index', f"{index:>2}: "), # Right-align index in 2 chars
        ('class:filename', item.filename),
        ('', ' ['),
        ('class:progress-bar-filled', '━' * filled_width),
        ('class:progress-bar', ' ' * empty_width),
        ('', '] '),
        ('class:percentage', percent_str),
        ('', ' '),
        ('class:size', f"({size_str})"),
        ('', '\n')
    ]
    return line_tuples

def get_download_list_content() -> List[Tuple[str, str]]:
    """ Returns the complete list of (style, text) tuples for the download status window. """
    global last_exception
    all_lines: List[Tuple[str, str]] = []
    try:
        # Get items and sort them consistently for display AND for index lookup later
        items = sorted(manager.get_items(), key=lambda x: x.filename)

        if not items:
             all_lines.append(('', "No downloads yet. Press 'a' to add a URL.\n"))
        else:
            # Use enumerate to get 1-based index for display
            for idx, item in enumerate(items, start=1):
                if item.status == 'downloading':
                    # Pass index to the progress bar function
                    progress_line_tuples = create_manual_progress_bar_tuples(idx, item)
                    all_lines.extend(progress_line_tuples)
                else:
                    # Add index prefix to non-downloading items
                    style_class = f"class:status-{item.status}"
                    base_text = str(item) # Uses DownloadItem.__str__
                    all_lines.append(('class:item-index', f"{idx:>2}: ")) # Index prefix
                    all_lines.append((style_class, f"{base_text}\n")) # Rest of the line

        if last_exception:
           all_lines.append(('', "\n"))
           all_lines.append(('class:error-message', f"Error: {last_exception}\n"))
           last_exception = None

        return all_lines

    except Exception as e:
        logging.exception("Error in get_download_list_content") # Log full trace
        last_exception = e
        return [('class:error-message', f"Error retrieving status: {e}\n")]


def get_prompt_message_content() -> List[Tuple[str, str]]:
    return [('class:prompt-message', prompt_message)]

def get_status_bar_content() -> List[Tuple[str, str]]:
    return [('class:status-bar', f" {status_message} ")]

# --- Input Buffer (Unchanged) ---
input_buffer = Buffer()

# --- Key Bindings ---
bindings = KeyBindings()

def reset_input_state(app_layout):
    global current_input_mode, prompt_message, status_message
    input_buffer.reset()
    current_input_mode = InputMode.COMMAND
    prompt_message = ""
    # Reset status message
    status_message = "Keys: [a]dd [p]ause# [r]esume# [P]auseAll [R]esumeAll [q]uit | Esc: cancel"
    app_layout.focus(download_list_window) # Focus main window

# --- Key Bindings Modifications ---

@bindings.add('a', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    # (Unchanged)
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_URL
    prompt_message = "Enter URL to add:"
    status_message = "Type URL and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('p', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'p' pressed: Prepare to enter number to pause. """
    global current_input_mode, prompt_message, status_message
    # Check if there are items first
    if not manager.get_items():
        status_message = "No downloads to pause."
        return

    current_input_mode = InputMode.ENTERING_PAUSE_NUMBER # Use new mode
    prompt_message = "Enter # to PAUSE:" # Update prompt
    status_message = "Type item number and press Enter. Esc to cancel." # Update status help
    input_buffer.reset()
    event.app.layout.focus(input_window)

@bindings.add('r', filter=Condition(lambda: current_input_mode == InputMode.COMMAND))
def _(event):
    """ 'r' pressed: Prepare to enter number to resume. """
    global current_input_mode, prompt_message, status_message
    # Check if there are items first
    if not manager.get_items():
        status_message = "No downloads to resume."
        return

    current_input_mode = InputMode.ENTERING_RESUME_NUMBER # Use new mode
    prompt_message = "Enter # to RESUME:" # Update prompt
    status_message = "Type item number and press Enter. Esc to cancel." # Update status help
    input_buffer.reset()
    event.app.layout.focus(input_window)

# ... (P, R, q, c-c, c-q bindings unchanged) ...
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
    logging.info("Exit requested. Stopping manager...")
    global status_message
    status_message = "Exiting..."
    manager.stop(graceful=True)
    event.app.exit()


@bindings.add('enter', filter=HasFocus(input_buffer) & Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    """ Handle processing input based on current_input_mode """
    global status_message, last_exception
    entered_text = input_buffer.text.strip()
    last_exception = None
    processed = False # Flag if input was handled

    try:
        if current_input_mode == InputMode.ENTERING_URL:
            if entered_text:
                if manager.add_download(entered_text): status_message = f"Added URL."
                else: status_message = f"Failed to add (duplicate?)."
            else: status_message = "Add cancelled."
            processed = True

        elif current_input_mode in [InputMode.ENTERING_PAUSE_NUMBER, InputMode.ENTERING_RESUME_NUMBER]:
            if not entered_text:
                 if current_input_mode == InputMode.ENTERING_PAUSE_NUMBER: status_message = "Pause cancelled."
                 else: status_message = "Resume cancelled."
                 processed = True
            else:
                try:
                    item_num = int(entered_text)
                    # Get current items list, sorted exactly as displayed
                    current_items = sorted(manager.get_items(), key=lambda x: x.filename)
                    num_items = len(current_items)

                    if 1 <= item_num <= num_items:
                        # Convert 1-based display index to 0-based list index
                        item_index = item_num - 1
                        target_item = current_items[item_index]
                        filename_to_modify = target_item.filename

                        if current_input_mode == InputMode.ENTERING_PAUSE_NUMBER:
                            if manager.pause_download(filename_to_modify):
                                status_message = f"Signalling pause for #{item_num} ('{filename_to_modify}')..."
                            else:
                                status_message = f"Cannot pause #{item_num} (status: {target_item.status})."
                            processed = True
                        elif current_input_mode == InputMode.ENTERING_RESUME_NUMBER:
                            if manager.resume_download(filename_to_modify):
                                status_message = f"Signalling resume for #{item_num} ('{filename_to_modify}')..."
                            else:
                                status_message = f"Cannot resume #{item_num} (status: {target_item.status})."
                            processed = True
                    else:
                        status_message = f"Invalid item number: {item_num}. Max is {num_items}."
                        processed = True # Handled the input, even though it was invalid

                except ValueError:
                    status_message = f"Invalid input: '{entered_text}'. Please enter a number."
                    processed = True # Handled the input

    except Exception as e:
        logging.exception(f"Error processing input '{entered_text}'")
        status_message = f"Error processing input!"
        last_exception = e

    # Reset state only if the input mode was handled or cancelled
    if processed or not entered_text:
        reset_input_state(event.app.layout)
    # else: keep prompt open if e.g. pause failed due to wrong status? (Decided against this for now)


@bindings.add('escape', filter=Condition(lambda: current_input_mode != InputMode.COMMAND))
def _(event):
    # (Unchanged)
    global status_message
    status_message = "Input cancelled."
    reset_input_state(event.app.layout)


# --- Layout Definition (Unchanged) ---
download_list_window = Window(
    content=FormattedTextControl(
        text=get_download_list_content,
        focusable=True
    ),
    wrap_lines=False
)
# ... rest of layout ...
prompt_message_window = Window(
    content=FormattedTextControl(text=get_prompt_message_content),
    height=1,
    align=WindowAlign.LEFT
)
input_window = Window(
    content=BufferControl(buffer=input_buffer, focusable=True),
    height=1,
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
root_container = HSplit([
    download_list_window,
    Window(height=1, char='─', style='class:separator'),
    input_area,
    status_bar_window
])
layout = Layout(root_container, focused_element=download_list_window)

# --- Application Setup (Unchanged) ---
app = Application(
    layout=layout,
    key_bindings=bindings,
    style=ui_style,
    full_screen=True,
    mouse_support=True,
    refresh_interval=0.5
)

# --- Signal Handling & Main Execution (Unchanged) ---
# ... (handle_sigterm, main, __main__ block) ...
def handle_sigterm(signum, frame):
    logging.info("SIGTERM received. Stopping manager and exiting...")
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

    # Optional: Clear log file on start
    # with open("downloader.log", 'w') as f: f.write("--- Log Start ---\n")

    logging.info("Starting download manager worker...")
    manager.start()

    logging.info("Launching TUI...")
    await app.run_async()

    logging.info("TUI exited. Final shutdown procedures...")
    if manager.worker_thread and manager.worker_thread.is_alive():
         logging.info("Ensuring download manager worker is stopped...")
         manager.stop(graceful=False)

if __name__ == "__main__":
    # Setup basic logging (can be configured further)
    log_level = os.environ.get("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s',
        filename="downloader.log", # Log to file
        filemode='a'
    )

    if sys.platform == "win32":
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.warning("KeyboardInterrupt caught in main. Forcing exit.")
        if manager: manager.stop(graceful=False)
    finally:
        logging.info("Application finished.")
