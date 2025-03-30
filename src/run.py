# run.py
import sys
import os
import signal
import asyncio
from enum import Enum, auto
from typing import List, Tuple, Set, Optional

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
# Import necessary layout components
from prompt_toolkit.layout.containers import (
    HSplit, VSplit, Window, WindowAlign, ConditionalContainer, FloatContainer, Float
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.filters import HasFocus, Condition, is_done, to_filter
from prompt_toolkit.styles import Style
# Import Frame for modal border
from prompt_toolkit.widgets import Frame
import logging

from dotenv import load_dotenv

# Assume download_manager exists and import directly
from download_manager import DownloadManager, DownloadItem
# Import Real-Debrid client and related items
from real_debrid import RealDebridClient, RealDebridDownload, RealDebridError, TokenError

# --- Constants ---
PROGRESS_BAR_WIDTH = 25

# --- Global Variables & State ---
manager = DownloadManager()
last_exception = None

# --- Input Modes ---
class InputMode(Enum):
    COMMAND = auto()
    ENTERING_URL = auto()
    ENTERING_PAUSE_NUMBER = auto()
    ENTERING_RESUME_NUMBER = auto()
    # No specific mode needed for RD modal, use rd_modal_active flag

current_input_mode = InputMode.COMMAND
status_message = "Keys: [a]dd [p]# [r]# [A]dd RD [P]All [R]All [q]uit | Esc: cancel"
prompt_message = ""

# --- Real-Debrid Modal State ---
rd_client: Optional[RealDebridClient] = None # Initialize later if needed
rd_modal_active = False
rd_downloads_list: List[RealDebridDownload] = []
rd_selected_indices: Set[int] = set() # Store indices of selected items
rd_current_index: int = 0 # Cursor position within the modal list

# --- UI Styling ---
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
    'item-index': 'fg:yellow',
    'prompt-message': 'fg:yellow',
    'status-bar': 'bg:#222222 fg:white',
    'separator': 'fg:#666666',
    'error-message': 'fg:ansired',
    # --- Modal Styles ---
    'modal-frame': 'bg:#333333',
    'modal-title': 'fg:yellow bold',
    'modal-text': 'fg:white',
    'modal-highlight': 'bg:ansiblue fg:white', # Cursor highlight
    'modal-selected': 'fg:ansigreen bold', # Selection marker
    'modal-unselected': 'fg:gray',       # Selection marker
    
    # Speed
    'speed': 'fg:ansimagenta',
    'eta': 'fg:ansiyellow',
})

# --- UI Content Functions ---

def format_size(byte_size: int) -> str:
    # (Unchanged)
    if byte_size < 1024: return f"{byte_size} B"
    if byte_size < 1024**2: return f"{byte_size/1024:.1f} KB"
    if byte_size < 1024**3: return f"{byte_size/(1024**2):.2f} MB"
    return f"{byte_size/(1024**3):.2f} GB"

def format_speed_rate(bps: float) -> str:
    """ Formats bytes per second into a human-readable rate. """
    if bps < 1024: return f"{bps:.0f} B/s"
    if bps < 1024**2: return f"{bps/1024:.1f} KB/s"
    if bps < 1024**3: return f"{bps/(1024**2):.2f} MB/s"
    return f"{bps/(1024**3):.2f} GB/s"

def format_eta(seconds: Optional[float]) -> str:
    """ Formats seconds into a human-readable ETA string (d, h, m, s). """
    if seconds is None or seconds < 0:
        return "N/A"
    if seconds == 0:
         return "Done" # Or "" ?
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s}s"
    if seconds < 86400: # Less than a day
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"
    # Over a day
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def create_manual_progress_bar_tuples(index: int, item: DownloadItem) -> List[Tuple[str, str]]:
    """ Creates tuples for ONE line representing a progress bar, including index, speed, and ETA. """
    percentage = 0.0
    if item.total_size > 0:
        percentage = min(100.0, (item.downloaded_size / item.total_size) * 100)

    filled_width = int(PROGRESS_BAR_WIDTH * percentage / 100)
    empty_width = max(0, PROGRESS_BAR_WIDTH - filled_width)

    size_str = f"{format_size(item.downloaded_size)}/{format_size(item.total_size)}" if item.total_size > 0 else f"{format_size(item.downloaded_size)}"
    percent_str = f"{percentage:.1f}%"

    # --- Get Speed and ETA ---
    speed_str = format_speed_rate(item.current_speed)
    eta_str = format_eta(item.eta_seconds)

    line_tuples = [
        ('class:item-index', f"{index:>2}: "),
        ('class:filename', item.filename),
        ('', ' ['),
        ('class:progress-bar-filled', '━' * filled_width),
        ('class:progress-bar', ' ' * empty_width),
        ('', '] '),
        ('class:percentage', percent_str),
        ('', ' '),
        ('class:speed', f"{speed_str:<10}"), # Add speed, pad for alignment
        ('', ' '),
        ('class:eta', f"ETA: {eta_str:<8}"), # Add ETA, pad
        ('', ' '),
        ('class:size', f"({size_str})"),
        ('', '\n')
    ]
    return line_tuples

def get_download_list_content() -> List[Tuple[str, str]]:
    # (Unchanged, except for potential error logging)
    global last_exception
    all_lines: List[Tuple[str, str]] = []
    try:
        items = manager.get_items()
        if not items: all_lines.append(('', "No downloads yet. [a]dd URL or [A]dd from Real-Debrid.\n"))
        else:
            for idx, item in enumerate(items, start=1):
                if item.status == 'downloading':
                    progress_line_tuples = create_manual_progress_bar_tuples(idx, item)
                    all_lines.extend(progress_line_tuples)
                else:
                    style_class = f"class:status-{item.status}"
                    base_text = str(item)
                    all_lines.append(('class:item-index', f"{idx:>2}: "))
                    all_lines.append((style_class, f"{base_text}\n"))
        if last_exception:
           all_lines.append(('', "\n"))
           all_lines.append(('class:error-message', f"Error: {last_exception}\n"))
           last_exception = None
        return all_lines
    except Exception as e:
        logging.exception("Error in get_download_list_content")
        last_exception = e
        return [('class:error-message', f"Error retrieving status: {e}\n")]

# --- Real-Debrid Modal Content ---
def get_rd_modal_content() -> List[Tuple[str, str]]:
    """ Generates the FormattedText list for the Real-Debrid download selection modal. """
    modal_lines: List[Tuple[str, str]] = []
    if not rd_downloads_list:
        modal_lines.append(('class:modal-text', " No downloads found on Real-Debrid.\n"))
        return modal_lines

    for i, item in enumerate(rd_downloads_list):
        is_selected = i in rd_selected_indices
        is_highlighted = i == rd_current_index

        # Determine style for the line based on highlight
        line_style = 'class:modal-highlight' if is_highlighted else 'class:modal-text'

        # Selection marker
        marker = "[x]" if is_selected else "[ ]"
        marker_style = 'class:modal-selected' if is_selected else 'class:modal-unselected'

        # Build the line tuples
        modal_lines.append((line_style, ' ')) # Indent
        modal_lines.append((marker_style, marker))
        modal_lines.append((line_style, f" {item.filename} "))
        modal_lines.append((line_style + ' class:size', f"({format_size(item.filesize)})")) # Combine styles
        modal_lines.append((line_style, '\n')) # Newline

    return modal_lines


def get_prompt_message_content() -> List[Tuple[str, str]]:
    return [('class:prompt-message', prompt_message)]

def get_status_bar_content() -> List[Tuple[str, str]]:
    return [('class:status-bar', f" {status_message} ")]

# --- Input Buffer (Unchanged) ---
input_buffer = Buffer()

# --- Key Bindings ---
# Main bindings (when not in modal or text input)
main_bindings = KeyBindings()

# --- Helper Functions ---
def reset_text_input_state(app_layout):
    """ Resets state after URL/Number input (previously reset_input_state) """
    global current_input_mode, prompt_message, status_message
    input_buffer.reset()
    current_input_mode = InputMode.COMMAND
    prompt_message = ""
    status_message = "Keys: [a]dd [p]# [r]# [A]dd RD [P]All [R]All [q]uit | Esc: cancel"
    app_layout.focus(download_list_window)

def reset_rd_modal_state(app_layout):
    """ Deactivates and resets the RD modal state. """
    global rd_modal_active, rd_downloads_list, rd_selected_indices, rd_current_index, status_message
    rd_modal_active = False
    rd_downloads_list = []
    rd_selected_indices = set()
    rd_current_index = 0
    status_message = "Keys: [a]dd [p]# [r]# [A]dd RD [P]All [R]All [q]uit | Esc: cancel" # Reset help text
    # Focus back on the main list window
    app_layout.focus(download_list_window)

# --- Main Command Bindings ---
@main_bindings.add('a', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
def _(event):
    global current_input_mode, prompt_message, status_message
    current_input_mode = InputMode.ENTERING_URL
    prompt_message = "Enter URL to add:"
    status_message = "Type URL and press Enter. Esc to cancel."
    input_buffer.reset()
    event.app.layout.focus(input_window)

@main_bindings.add('p', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
def _(event):
    global current_input_mode, prompt_message, status_message
    if not manager.get_items(): status_message = "No downloads to pause."; return
    current_input_mode = InputMode.ENTERING_PAUSE_NUMBER
    prompt_message = "Enter # to PAUSE:"; status_message = "Type item number and press Enter. Esc to cancel."
    input_buffer.reset(); event.app.layout.focus(input_window)

@main_bindings.add('r', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
def _(event):
    global current_input_mode, prompt_message, status_message
    if not manager.get_items(): status_message = "No downloads to resume."; return
    current_input_mode = InputMode.ENTERING_RESUME_NUMBER
    prompt_message = "Enter # to RESUME:"; status_message = "Type item number and press Enter. Esc to cancel."
    input_buffer.reset(); event.app.layout.focus(input_window)

@main_bindings.add('A', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active)) # Shift+A
def _(event):
    """ Fetch RD downloads and show modal """
    global rd_client, rd_downloads_list, rd_modal_active, status_message, rd_selected_indices, rd_current_index
    global last_exception

    status_message = "Fetching Real-Debrid downloads..."
    event.app.invalidate() # Trigger redraw to show status message

    try:
        if rd_client is None:
            rd_client = RealDebridClient() # Initialize only once or on demand

        rd_downloads_list = rd_client.get_downloads()
        rd_selected_indices = set() # Reset selection
        rd_current_index = 0        # Reset cursor

        if not rd_downloads_list:
            status_message = "No downloads found on Real-Debrid."
            # Optionally show modal anyway with the message? Or just stay in main view.
            # Let's show the modal with the message for consistency.
            # rd_modal_active = True # Activate even if empty
            # event.app.layout.focus(rd_modal_window) # Focus the (empty) modal
        # else: # Activate only if list is not empty
        rd_modal_active = True
        status_message = "Select RD downloads: [Space] toggle, [Enter] add selected, [Esc] cancel"
        event.app.layout.focus(rd_modal_window) # Focus the modal window control


    except TokenError as e:
        logging.error(f"Real-Debrid Token Error: {e}")
        status_message = f"RD Token Error: {e}"
        last_exception = e
        reset_rd_modal_state(event.app.layout) # Ensure modal is closed on error
    except RealDebridError as e:
        logging.error(f"Real-Debrid API Error: {e}")
        status_message = f"RD API Error: {e}"
        last_exception = e
        reset_rd_modal_state(event.app.layout)
    except Exception as e: # Catch unexpected errors during init or fetch
        logging.exception("Unexpected error fetching RD downloads")
        status_message = "Unexpected error fetching RD downloads."
        last_exception = e
        reset_rd_modal_state(event.app.layout)


@main_bindings.add('P', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
def _(event):
    global status_message
    if manager.pause_all(): status_message = "Signalled all active downloads to pause."
    else: status_message = "No active downloads to pause."

@main_bindings.add('R', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
def _(event):
    global status_message
    if manager.resume_all(): status_message = "Signalled all paused downloads to resume."
    else: status_message = "No paused downloads to resume."

@main_bindings.add('q', filter=Condition(lambda: current_input_mode == InputMode.COMMAND and not rd_modal_active))
@main_bindings.add('c-c') # Global exit
@main_bindings.add('c-q') # Global exit
def _(event):
    # (Unchanged)
    logging.info("Exit requested. Stopping manager...")
    global status_message; status_message = "Exiting..."
    manager.stop(graceful=True); event.app.exit()


# --- Text Input Field Bindings (URL/Number) ---
@main_bindings.add('enter', filter=HasFocus(input_buffer) & Condition(lambda: current_input_mode != InputMode.COMMAND and not rd_modal_active))
def _(event):
    # (Logic largely unchanged, just uses reset_text_input_state now)
    global status_message, last_exception
    entered_text = input_buffer.text.strip()
    last_exception = None; processed = False
    try:
        if current_input_mode == InputMode.ENTERING_URL:
            if entered_text:
                if manager.add_download(entered_text): status_message = f"Added URL."
                else: status_message = f"Failed to add (duplicate?)."
            else: status_message = "Add cancelled."
            processed = True
        elif current_input_mode in [InputMode.ENTERING_PAUSE_NUMBER, InputMode.ENTERING_RESUME_NUMBER]:
             if not entered_text:
                 status_message = "Input cancelled."
                 processed = True
             else:
                try:
                    item_num = int(entered_text)
                    current_items = sorted(manager.get_items(), key=lambda x: x.filename)
                    num_items = len(current_items)
                    if 1 <= item_num <= num_items:
                        item_index = item_num - 1; target_item = current_items[item_index]
                        filename = target_item.filename
                        if current_input_mode == InputMode.ENTERING_PAUSE_NUMBER:
                            if manager.pause_download(filename): status_message = f"Pausing #{item_num}..."
                            else: status_message = f"Cannot pause #{item_num} (status: {target_item.status})."
                        else: # RESUME
                            if manager.resume_download(filename): status_message = f"Resuming #{item_num}..."
                            else: status_message = f"Cannot resume #{item_num} (status: {target_item.status})."
                    else: status_message = f"Invalid item number: {item_num}. Max is {num_items}."
                    processed = True
                except ValueError:
                    status_message = f"Invalid input: '{entered_text}'. Enter a number."
                    processed = True
    except Exception as e:
        logging.exception(f"Error processing text input '{entered_text}'")
        status_message = f"Error processing input!"; last_exception = e
    if processed or not entered_text: reset_text_input_state(event.app.layout)

@main_bindings.add('escape', filter=Condition(lambda: current_input_mode != InputMode.COMMAND and not rd_modal_active))
def _(event):
    status_message = "Input cancelled."
    reset_text_input_state(event.app.layout)


# --- Real-Debrid Modal Bindings ---
# These bindings are only active when rd_modal_active is True
rd_modal_filter = Condition(lambda: rd_modal_active)

# Modal specific bindings
rd_modal_bindings = KeyBindings()

@rd_modal_bindings.add('escape', filter=rd_modal_filter)
@rd_modal_bindings.add('q', filter=rd_modal_filter)
def _(event):
    """ Cancel RD selection """
    reset_rd_modal_state(event.app.layout)

@rd_modal_bindings.add('up', filter=rd_modal_filter)
@rd_modal_bindings.add('k', filter=rd_modal_filter)
def _(event):
    """ Move cursor up in RD list """
    global rd_current_index
    if rd_downloads_list: # Only move if list is not empty
        rd_current_index = (rd_current_index - 1) % len(rd_downloads_list)

@rd_modal_bindings.add('down', filter=rd_modal_filter)
@rd_modal_bindings.add('j', filter=rd_modal_filter)
def _(event):
    """ Move cursor down in RD list """
    global rd_current_index
    if rd_downloads_list:
        rd_current_index = (rd_current_index + 1) % len(rd_downloads_list)

@rd_modal_bindings.add('space', filter=rd_modal_filter)
def _(event):
    """ Toggle selection of the current item in RD list """
    global rd_selected_indices
    if rd_downloads_list: # Only toggle if list is not empty
        if rd_current_index in rd_selected_indices:
            rd_selected_indices.remove(rd_current_index)
        else:
            rd_selected_indices.add(rd_current_index)

@rd_modal_bindings.add('enter', filter=rd_modal_filter)
def _(event):
    """ Add selected RD downloads to the main download manager """
    global status_message
    added_count = 0
    skipped_count = 0
    if rd_selected_indices:
        status_message = f"Adding {len(rd_selected_indices)} selected downloads..."
        event.app.invalidate() # Show status update immediately

        # Iterate through a copy of the indices to avoid issues if set changes (shouldn't here)
        for index in list(rd_selected_indices):
            if 0 <= index < len(rd_downloads_list):
                item_to_add = rd_downloads_list[index]
                # Use the actual download URL
                if manager.add_download(item_to_add.download_url):
                    added_count += 1
                else:
                    skipped_count += 1 # e.g., duplicate filename

        status_message = f"Added {added_count} RD items."
        if skipped_count > 0:
            status_message += f" Skipped {skipped_count} (duplicates?)."
    else:
        status_message = "No RD items selected."

    reset_rd_modal_state(event.app.layout) # Close modal after adding


# --- Layout Definition ---

# --- Main download list window ---
download_list_window = Window(
    content=FormattedTextControl(text=get_download_list_content, focusable=True),
    wrap_lines=False
)

# --- Input area (prompt + text field) ---
prompt_message_window = Window(content=FormattedTextControl(text=get_prompt_message_content), height=1, align=WindowAlign.LEFT)
input_window = Window(content=BufferControl(buffer=input_buffer, focusable=True), height=1)
input_area = ConditionalContainer(
    content=HSplit([prompt_message_window, input_window]),
    filter=Condition(lambda: current_input_mode != InputMode.COMMAND and not rd_modal_active) # Hide if modal is active too
)

# --- Status bar ---
status_bar_window = Window(content=FormattedTextControl(text=get_status_bar_content), height=1, align=WindowAlign.LEFT)

# --- Real-Debrid Modal Window ---
# This is the actual window *inside* the Float
rd_modal_window = Window(
    content=FormattedTextControl(
        text=get_rd_modal_content, # Use the specific modal content function
        focusable=True,            # Make it focusable for keybindings
        key_bindings=rd_modal_bindings # Attach modal-specific bindings HERE
    ),
    # Style applies to the window background if needed, Frame handles border
    # style='class:modal-content',
    # Allow scrolling if content overflows
    wrap_lines=False,
    # Essential for scrolling long lists
    # dont_extend_height=False,
    # dont_extend_width=False,
)

# Frame adds border and title around the modal window
rd_modal_frame = Frame(
    title=lambda: f"Real-Debrid Downloads ({len(rd_selected_indices)} selected)", # Dynamic title
    body=rd_modal_window,
    style='class:modal-frame', # Apply style to frame
    modal=True # Important: Makes it behave like a modal dialog for focus
)

# --- Root Container with Float ---
# The main layout remains largely the same HSplit
main_container = HSplit([
    download_list_window,
    Window(height=1, char='─', style='class:separator'),
    input_area, # Conditional input area
    status_bar_window
])

# FloatContainer holds the main layout AND the Float(s)
root_container = FloatContainer(
    content=main_container, # The standard layout goes here
    floats=[
        # The Float positions the modal Frame
        Float(
            content=ConditionalContainer( # Show/hide the Frame based on state
                 content=rd_modal_frame,
                 filter=Condition(lambda: rd_modal_active)
            ),
            # Position the float (optional, defaults usually ok)
            # top=2, bottom=2, left=5, right=5 # Example positioning
        )
    ]
)

# Final Layout
layout = Layout(root_container, focused_element=download_list_window) # Initial focus on main list


# --- Application Setup ---
# Merge all key bindings
# Bindings attached directly to controls (like rd_modal_window) take precedence when focused
# Then bindings in the Application take effect based on filters
application_bindings = merge_key_bindings([main_bindings, rd_modal_bindings])

app = Application(
    layout=layout,
    key_bindings=application_bindings, # Use merged bindings
    style=ui_style,
    full_screen=True,
    mouse_support=True, # Mouse can click modal elements too
    refresh_interval=0.5
)

# --- Signal Handling & Main Execution (Unchanged) ---
# ... (handle_sigterm, main, __main__ block) ...
def handle_sigterm(signum, frame):
    logging.info("SIGTERM received. Stopping manager and exiting...")
    global status_message; status_message = "SIGTERM received, exiting..."
    manager.stop(graceful=True)
    try: loop = asyncio.get_running_loop(); loop.call_soon_threadsafe(app.exit)
    except RuntimeError: sys.exit(1)

async def main():
    load_dotenv()

    loop = asyncio.get_running_loop()
    if hasattr(signal, "SIGTERM"): loop.add_signal_handler(signal.SIGTERM, handle_sigterm, signal.SIGTERM, None)
    log_level = os.environ.get("LOGLEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s [%(threadName)s] %(name)s: %(message)s', filename="downloader.log", filemode='a')
    if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    logging.info("Starting download manager worker...")
    manager.start()
    logging.info("Launching TUI...")
    await app.run_async()
    logging.info("TUI exited. Final shutdown procedures...")
    if manager.worker_thread and manager.worker_thread.is_alive():
         logging.info("Ensuring download manager worker is stopped...")
         manager.stop(graceful=False)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logging.warning("KeyboardInterrupt caught. Forcing exit."); manager.stop(graceful=False)
    finally: logging.info("Application finished.")
