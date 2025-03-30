import sys
import os
import signal
import asyncio
from typing import List

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI

from download_manager import DownloadManager, DownloadItem 

# --- Global Variables ---
manager = DownloadManager()
status_message = "Ready. Commands: add <url>, pause <fn>, resume <fn>, list, exit/quit/q"
last_exception = None # To display errors briefly

# --- UI Content Functions ---

def get_download_list_content():
    """ Returns FormattedTextList for the download status window. """
    global last_exception
    try:
        # Use the existing get_status method, but wrap its output in ANSI
        # for prompt_toolkit compatibility if it contained raw escape codes.
        # If get_status just returns plain text, ANSI() is fine.
        status_str = manager.get_status()
        # Ensure list updates if an exception occurred previously
        if last_exception:
           status_str += f"\n\nError processing command: {last_exception}"
           last_exception = None # Clear after displaying once
        return ANSI(status_str) # Use ANSI to handle potential raw strings safely
    except Exception as e:
        # Avoid crashing the UI if get_status fails, display error instead
        last_exception = e
        return ANSI(f"Error retrieving status: {e}")

def get_status_bar_content():
    """ Returns FormattedTextList for the bottom status bar. """
    # Display the persistent status message
    # Could potentially add dynamic info here later (e.g., total speed)
    return ANSI(status_message)

# --- Key Bindings ---
bindings = KeyBindings()

@bindings.add('c-c')
@bindings.add('c-q')
def _(event):
    """ Exit application on Ctrl+C or Ctrl+Q. """
    print("\nCtrl+C/Q detected. Stopping manager and exiting...")
    manager.stop(graceful=True) # Attempt graceful shutdown
    event.app.exit()

# --- Input Buffer Handling ---
input_buffer = Buffer() # We don't assign accept_handler here, handle in 'enter' binding

def handle_command(command_text: str):
    """ Parses and executes commands entered by the user. """
    global status_message, last_exception
    last_exception = None # Clear previous error on new command
    command_parts = command_text.strip().split()
    if not command_parts:
        return

    action = command_parts[0].lower()

    try:
        if action in ["exit", "quit", "q"]:
            manager.stop(graceful=True)
            # Find the application instance to exit it
            # This is a bit indirect, usually you'd have app access in event handlers
            # A workaround is to schedule the exit via the loop
            loop = asyncio.get_event_loop()
            loop.call_soon(lambda: asyncio.ensure_future(app.exit())) # Schedule exit
            status_message = "Exiting..."

        elif action == "add" and len(command_parts) > 1:
            url = command_parts[1]
            manager.add_download(url)
            status_message = f"Added '{url[:50]}{'...' if len(url)>50 else ''}' to queue."

        elif action == "pause" and len(command_parts) > 1:
            filename = " ".join(command_parts[1:])
            if manager.pause_download(filename):
                status_message = f"Attempting to pause '{filename}'..."
            else:
                # pause_download prints its own messages for failures
                item = manager.downloads.get(filename)
                if item:
                    status_message = f"Could not pause '{filename}' (status: {item.status})."
                else:
                    status_message = f"Download '{filename}' not found."


        elif action == "resume" and len(command_parts) > 1:
            filename = " ".join(command_parts[1:])
            if manager.resume_download(filename):
                status_message = f"Resuming '{filename}'..."
            else:
                # resume_download prints its own messages for failures
                 item = manager.downloads.get(filename)
                 if item:
                    status_message = f"Could not resume '{filename}' (status: {item.status})."
                 else:
                    status_message = f"Download '{filename}' not found."

        elif action == "list":
            # List is always displayed, this command is redundant but harmless
            status_message = "Displaying download list."

        else:
            status_message = f"Unknown command: '{action}'. Use: add, pause, resume, list, exit"

    except Exception as e:
        print(f"\nError processing command '{command_text}': {e}") # Also print to console for debug
        status_message = f"Error: {e}"
        last_exception = e # Store for display in main window


# Bind Enter key to handle the command
@bindings.add('enter')
def _(event):
    """ Handle command submission when Enter is pressed. """
    command = input_buffer.text
    handle_command(command)
    input_buffer.reset() # Clear the input buffer

# --- Layout Definition ---

# Window to display the download list
download_list_window = Window(
    content=FormattedTextControl(text=get_download_list_content),
    wrap_lines=True
)

# Window for the user input command line
input_window = Window(
    content=BufferControl(buffer=input_buffer),
    height=1,
    style="class:input-field" # Optional styling
)

# Window for the status bar at the bottom
status_bar_window = Window(
    content=FormattedTextControl(text=get_status_bar_content),
    height=1,
    style="class:status-bar", # Optional styling
    align=WindowAlign.LEFT
)

# Horizontal split layout
root_container = HSplit([
    download_list_window,       # Main content area
    Window(height=1, char='-', style='class:separator'), # Separator line
    Window(height=1, char='-', style='class:separator'), # Separator line
    input_window,               # Command input line
    status_bar_window,           # Status message line
])

# Create the layout
layout = Layout(root_container, focused_element=input_window) # Focus input field initially


# --- Application Setup ---
app = Application(
    layout=layout,
    key_bindings=bindings,
    full_screen=True,
    mouse_support=True, # Enable mouse support (optional)
    refresh_interval=0.5 # Refresh the UI every 0.5 seconds to update status
)

# --- Signal Handling for Graceful Exit (Alternative/Robust) ---
def handle_sigterm(signum, frame):
    """ Handle SIGTERM signal for graceful shutdown. """
    print("\nSIGTERM received. Stopping manager and exiting...")
    manager.stop(graceful=True)
     # Ensure the application event loop stops cleanly
    try:
        loop = asyncio.get_running_loop()
        # Schedule the exit call within the loop's context
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(app.cancel()))
    except RuntimeError: # Loop not running
        sys.exit(1) # Exit directly if loop isn't running

# --- Main Execution ---
async def main():
    """ Asynchronous main function to run the application. """
    # Register signal handlers
    # SIGINT is usually handled by prompt_toolkit's default ctrl-c binding
    # Register SIGTERM for system shutdowns
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm, signal.SIGTERM, None)

    print("Starting download manager worker...")
    manager.start() # Start the background download worker

    print("Launching TUI...")
    await app.run_async() # Run the prompt_toolkit application

    print("TUI exited. Final shutdown.")
    # Ensure manager is stopped if not already done by exit command/signal
    if manager.worker_thread and manager.worker_thread.is_alive():
         manager.stop(graceful=True)


if __name__ == "__main__":
    # Setup basic logging or print statements if needed before TUI starts
    if sys.platform == "win32":
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt caught in main. Exiting.")
        # Ensure manager stop is called even if TUI crashes or is force-quit
        manager.stop(graceful=False) # Non-graceful stop might be needed here
    finally:
        print("Application finished.")
