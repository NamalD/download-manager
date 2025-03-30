import requests
import os
import threading
import queue
import time
import json
import signal
import sys
from urllib.parse import urlparse

# --- Configuration ---
CHUNK_SIZE = 8192  # Size of chunks to download (bytes)
DOWNLOAD_DIR = "downloads" # Directory to save downloads
STATE_FILE = "download_state.json" # File to save queue/progress

# --- Download Item Class ---
class DownloadItem:
    def __init__(self, url, filename=None, total_size=0, downloaded_size=0, status='queued', error_message=None):
        self.url = url
        self.filename = filename or self._generate_filename(url)
        self.temp_filename = os.path.join(DOWNLOAD_DIR, self.filename + ".part")
        self.final_filename = os.path.join(DOWNLOAD_DIR, self.filename)
        self.progress_file = os.path.join(DOWNLOAD_DIR, self.filename + ".progress") # Stores only downloaded_size

        self.total_size = total_size
        self.downloaded_size = downloaded_size
        self.status = status # queued, downloading, paused, completed, error
        self.error_message = error_message
        self.pause_event = threading.Event() # Used to signal pause request
        self.stop_event = threading.Event()  # Used to signal stop/exit request

    def _generate_filename(self, url):
        try:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                # Try to get from content disposition or just use domain+hash
                # For simplicity now, use a default or raise error
                return f"download_{hash(url)}.unknown"
            return filename
        except Exception:
            return f"download_{hash(url)}.unknown"

    def to_dict(self):
        # Convert object state to a dictionary for saving
        return {
            'url': self.url,
            'filename': self.filename,
            'total_size': self.total_size,
            'downloaded_size': self.downloaded_size,
            'status': self.status,
            'error_message': self.error_message,
        }

    @classmethod
    def from_dict(cls, data):
        # Create object from dictionary (loaded from state file)
        item = cls(
            url=data['url'],
            filename=data.get('filename'), # Use get for backward compatibility if needed
            total_size=data.get('total_size', 0),
            downloaded_size=data.get('downloaded_size', 0),
            status=data.get('status', 'queued'),
            error_message=data.get('error_message')
        )
        # Restore state for paused/interrupted downloads
        if item.status in ['paused', 'error', 'downloading']: # Treat 'downloading' as interrupted on load
             if os.path.exists(item.progress_file):
                 try:
                     with open(item.progress_file, 'r') as pf:
                         item.downloaded_size = int(pf.read().strip())
                     # If it was downloading or paused, mark as paused on load
                     if item.status == 'downloading':
                         item.status = 'paused'
                 except (IOError, ValueError):
                     print(f"Warning: Could not read progress for {item.filename}. Resetting download.")
                     item.downloaded_size = 0
                     item.status = 'queued' # Reset to queued if progress is corrupt
                     if os.path.exists(item.temp_filename):
                         os.remove(item.temp_filename) # Clean partial file
                     if os.path.exists(item.progress_file):
                         os.remove(item.progress_file) # Clean progress file
             else:
                 # If progress file is missing but status suggests it should exist
                 print(f"Warning: Progress file missing for {item.filename}. Resetting download.")
                 item.downloaded_size = 0
                 item.status = 'queued'
                 if os.path.exists(item.temp_filename):
                      os.remove(item.temp_filename)

        elif item.status == 'completed':
             # Verify final file exists, otherwise reset
             if not os.path.exists(item.final_filename):
                 print(f"Warning: Completed file {item.filename} missing. Resetting.")
                 item.status = 'queued'
                 item.downloaded_size = 0

        return item

    def __str__(self):
        if self.status == 'downloading' and self.total_size > 0:
            percent = (self.downloaded_size / self.total_size) * 100
            size_str = f"{self.downloaded_size / (1024*1024):.2f}/{self.total_size / (1024*1024):.2f} MB"
            return f"[{self.filename}] {self.status.upper()} {percent:.1f}% ({size_str})"
        elif self.status == 'downloading':
             size_str = f"{self.downloaded_size / (1024*1024):.2f} MB"
             return f"[{self.filename}] {self.status.upper()} ({size_str}, Total size unknown)"
        elif self.status == 'error':
            return f"[{self.filename}] {self.status.upper()} - {self.error_message}"
        else:
             size_str = f"{self.total_size / (1024*1024):.2f} MB" if self.total_size else "Size unknown"
             if self.status == 'completed':
                 return f"[{self.filename}] {self.status.upper()} ({size_str})"
             elif self.status == 'paused':
                 percent = (self.downloaded_size / self.total_size) * 100 if self.total_size else 0
                 return f"[{self.filename}] {self.status.upper()} at {percent:.1f}%"
             else: # queued
                 return f"[{self.filename}] {self.status.upper()}"

# --- Download Manager Class ---
class DownloadManager:
    def __init__(self):
        self.download_queue = queue.Queue()
        self.downloads = {} # Dictionary to store DownloadItem objects {filename: DownloadItem}
        self.lock = threading.Lock() # To protect access to self.downloads
        self.worker_thread = None
        self.stop_event = threading.Event() # Global stop signal for worker

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.load_state()

    def add_download(self, url):
        item = DownloadItem(url)
        if item.filename in self.downloads:
            print(f"Download for '{item.filename}' already exists.")
            return

        with self.lock:
            self.downloads[item.filename] = item
        self.download_queue.put(item)
        print(f"Added '{item.filename}' to the queue.")
        self.save_state() # Save state after adding

    def get_status(self):
        with self.lock:
            if not self.downloads:
                return "No downloads."
            status_lines = ["--- Download Status ---"]
            items = list(self.downloads.values()) # Create a copy for safe iteration

        # Sort for consistent display (e.g., by filename)
        items.sort(key=lambda x: x.filename)

        for item in items:
            status_lines.append(str(item)) # Use the __str__ method of DownloadItem
        return "\n".join(status_lines)

    def pause_download(self, filename):
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'downloading':
                item.pause_event.set() # Signal the download loop to pause
                print(f"Attempting to pause '{filename}'...")
                # Status will be updated to 'paused' by the worker thread
                # We don't save state here, worker saves when pausing
                return True
            elif item and item.status == 'paused':
                 print(f"'{filename}' is already paused.")
                 return False
            elif item:
                 print(f"Cannot pause '{filename}' (status: {item.status}).")
                 return False
            else:
                print(f"Download '{filename}' not found.")
                return False

    def resume_download(self, filename):
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'paused':
                item.status = 'queued' # Mark as queued again
                item.pause_event.clear() # Clear the pause signal
                self.download_queue.put(item) # Add back to the queue
                print(f"Resuming '{filename}'...")
                self.save_state() # Save the change in status
                return True
            elif item and item.status == 'downloading':
                 print(f"'{filename}' is already downloading.")
                 return False
            elif item and item.status == 'queued':
                 print(f"'{filename}' is already queued.")
                 return False
            elif item:
                 print(f"Cannot resume '{filename}' (status: {item.status}).")
                 return False
            else:
                print(f"Download '{filename}' not found.")
                return False

    def _worker(self):
        print("Download worker started.")
        while not self.stop_event.is_set():
            try:
                # Wait for an item, but timeout occasionally to check stop_event
                item = self.download_queue.get(timeout=1)
            except queue.Empty:
                continue # No items, loop back and check stop_event

            # Double-check if this item was asked to stop globally while queued
            if self.stop_event.is_set():
                 # Re-queue if necessary or mark as paused? Let's just break
                 # self.download_queue.put(item) # Potentially re-queue if needed
                 break

            # Check if this specific item received a stop signal while queued
            # (e.g., if cancel functionality were added)
            if item.stop_event.is_set():
                print(f"Skipping cancelled download: {item.filename}")
                self.download_queue.task_done()
                continue

            # Check if this item is already completed (e.g., loaded state)
            if item.status == 'completed':
                print(f"Skipping already completed download: {item.filename}")
                self.download_queue.task_done()
                continue

            print(f"Starting download: {item.filename}")
            self._process_download(item)
            self.download_queue.task_done()
            self.save_state() # Save state after each download finishes/pauses/errors

        print("Download worker stopped.")

    def _process_download(self, item):
        try:
            with self.lock:
                item.status = 'downloading'
                item.error_message = None # Clear previous errors
                item.pause_event.clear() # Ensure pause flag is clear initially

            # --- Get file info (size, resume support) ---
            headers = {}
            current_size = 0

            # Check existing progress file first
            if os.path.exists(item.progress_file):
                try:
                    with open(item.progress_file, 'r') as pf:
                        current_size = int(pf.read().strip())
                    item.downloaded_size = current_size # Update item's state
                    print(f"Found progress file for {item.filename}. Resuming from {current_size} bytes.")
                except (IOError, ValueError):
                    print(f"Warning: Could not read progress file for {item.filename}. Starting from scratch.")
                    current_size = 0
                    item.downloaded_size = 0
                    # Clean up potentially corrupted files
                    if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                    if os.path.exists(item.progress_file): os.remove(item.progress_file)

            # If resuming, set the Range header
            if current_size > 0:
                headers['Range'] = f'bytes={current_size}-'

            # Use stream=True to avoid loading the whole file into memory
            response = requests.get(item.url, headers=headers, stream=True, timeout=30) # Added timeout
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            # --- Check server response for resume ---
            is_resuming = False
            if current_size > 0:
                if response.status_code == 206: # Partial Content
                    print(f"Server supports resume for {item.filename}.")
                    is_resuming = True
                    # Update total size if Content-Range is present
                    content_range = response.headers.get('Content-Range')
                    if content_range:
                         try:
                              _, range_str = content_range.split() # e.g., bytes 1000-5000/5001
                              _, total = range_str.split('/')
                              item.total_size = int(total)
                         except ValueError:
                              print(f"Warning: Couldn't parse Content-Range: {content_range}")
                              # Keep existing total_size if we had it, otherwise it remains 0
                elif response.status_code == 200: # OK
                    print(f"Server did not support resume (sent full file). Restarting {item.filename}.")
                    current_size = 0
                    item.downloaded_size = 0
                    is_resuming = False
                    # Clean up old progress/part files if server forces restart
                    if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                    if os.path.exists(item.progress_file): os.remove(item.progress_file)
                else:
                    raise requests.exceptions.RequestException(f"Unexpected status code {response.status_code} during resume attempt.")

            # --- Get total size if not resuming or not already known ---
            if not is_resuming or item.total_size == 0:
                content_length = response.headers.get('content-length')
                if content_length:
                    item.total_size = int(content_length)
                    # If it's a full download, total_size *is* the content_length
                    # If it's a 206 without Content-Range, content_length is the *remaining* size
                    if is_resuming and item.total_size != (int(content_length) + current_size):
                        # This case is complex, try to derive total from Content-Range if possible (done above)
                        # If Content-Range wasn't usable, we might have an incorrect total_size.
                        # For simplicity, we'll proceed, but progress % might be wrong.
                        print(f"Warning: Content-Length ({content_length}) doesn't align perfectly with resume size ({current_size}). Total size might be approximate.")
                        # A potential fix is another HEAD request, but let's keep it simple for now.
                        item.total_size = int(content_length) + current_size # Best guess
                else:
                    item.total_size = 0 # Size unknown
                    print(f"Warning: Server did not provide Content-Length for {item.filename}.")

            # --- Download Loop ---
            file_mode = 'ab' if is_resuming else 'wb'
            last_progress_save_time = time.time()

            with open(item.temp_filename, file_mode) as f:
                if is_resuming:
                    f.seek(current_size) # Should be redundant with 'ab', but ensures position

                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    # --- Check for Pause or Stop signals ---
                    if item.pause_event.is_set() or item.stop_event.is_set() or self.stop_event.is_set():
                        with self.lock:
                            item.status = 'paused'
                        print(f"\nPausing download: {item.filename} at {item.downloaded_size} bytes.")
                        # Save progress before pausing
                        try:
                            with open(item.progress_file, 'w') as pf:
                                pf.write(str(item.downloaded_size))
                        except IOError as e:
                             print(f"Error saving progress for {item.filename} on pause: {e}")
                        return # Exit processing this item

                    if chunk: # filter out keep-alive new chunks
                        f.write(chunk)
                        item.downloaded_size += len(chunk)

                        # --- Save progress periodically ---
                        current_time = time.time()
                        if current_time - last_progress_save_time > 5: # Save every 5 seconds
                            try:
                                with open(item.progress_file, 'w') as pf:
                                    pf.write(str(item.downloaded_size))
                                last_progress_save_time = current_time
                            except IOError as e:
                                print(f"\nError saving progress for {item.filename}: {e}")
                                # Decide if we should stop the download on persistent save errors
                                # For now, we continue, but it risks losing progress on crash

                        # --- Display Progress ---
                        if item.total_size > 0:
                            percent = (item.downloaded_size / item.total_size) * 100
                            # Use \r to update the line in place
                            print(f"\rDownloading {item.filename}: {percent:.1f}% ({item.downloaded_size}/{item.total_size})", end="")
                        else:
                            print(f"\rDownloading {item.filename}: {item.downloaded_size} bytes (Total size unknown)", end="")

            # --- Download Complete ---
            # Final progress save ensures the correct final size is recorded before rename
            try:
                with open(item.progress_file, 'w') as pf:
                    pf.write(str(item.downloaded_size))
            except IOError as e:
                 print(f"\nError saving final progress for {item.filename}: {e}")
                 # Proceed to rename, but log the error

            # Verify downloaded size matches total size if known
            if item.total_size > 0 and item.downloaded_size != item.total_size:
                 raise IOError(f"Download incomplete: Expected {item.total_size} bytes, got {item.downloaded_size}")

            # Rename temp file to final file
            os.rename(item.temp_filename, item.final_filename)
            # Clean up progress file
            if os.path.exists(item.progress_file):
                os.remove(item.progress_file)

            with self.lock:
                item.status = 'completed'
            print(f"\nDownload completed: {item.filename}")

        except requests.exceptions.RequestException as e:
            print(f"\nError downloading {item.filename}: {e}")
            with self.lock:
                item.status = 'error'
                item.error_message = str(e)
            # Keep partial file and progress file for potential resume later if it was a network error
            # Save current progress if possible
            try:
                 with open(item.progress_file, 'w') as pf:
                     pf.write(str(item.downloaded_size))
            except IOError as ioe:
                 print(f"Could not save progress during error for {item.filename}: {ioe}")

        except IOError as e:
            print(f"\nFile I/O error for {item.filename}: {e}")
            with self.lock:
                item.status = 'error'
                item.error_message = f"File I/O Error: {e}"
            # Don't save progress if it was an IO error writing the file itself

        except Exception as e:
            print(f"\nAn unexpected error occurred for {item.filename}: {e}")
            with self.lock:
                item.status = 'error'
                item.error_message = f"Unexpected Error: {e}"
            # Attempt to save progress here too
            try:
                 with open(item.progress_file, 'w') as pf:
                     pf.write(str(item.downloaded_size))
            except IOError as ioe:
                 print(f"Could not save progress during unexpected error for {item.filename}: {ioe}")

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            print("Worker thread already running.")
            return

        self.stop_event.clear()
        # Re-queue any downloads that were paused or interrupted ('error' could be resumable)
        with self.lock:
            for item in self.downloads.values():
                if item.status in ['paused', 'error']: # Add error state to potentially retry/resume
                     # Only re-queue if not already completed or actively downloading
                     if item.status != 'completed' and not (item.status == 'downloading' and not item.pause_event.is_set()):
                         # Make sure it's not already in the queue logic-wise
                         # Safest is just to attempt resume command logic
                         print(f"Preparing to resume/retry: {item.filename}")
                         item.status = 'queued' # Mark for queueing
                         item.pause_event.clear()
                         item.stop_event.clear()
                         self.download_queue.put(item)


        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def stop(self, graceful=True):
        print("Stopping download manager...")
        self.stop_event.set() # Signal global stop

        # Signal currently downloading item to pause/stop
        with self.lock:
            active_item = None
            for item in self.downloads.values():
                if item.status == 'downloading':
                    active_item = item
                    break
            if active_item:
                 print(f"Signalling active download '{active_item.filename}' to stop...")
                 active_item.stop_event.set() # Signal the specific item too

        if graceful and self.worker_thread and self.worker_thread.is_alive():
            print("Waiting for worker thread to finish current task gracefully...")
            # Wait for the worker to pause the current download and exit its loop
            self.worker_thread.join(timeout=15) # Wait up to 15 seconds
            if self.worker_thread.is_alive():
                 print("Worker thread did not stop gracefully. Forcing exit might be needed externally.")
            else:
                 print("Worker thread stopped.")

        # Ensure state is saved on exit
        print("Saving final state...")
        self.save_state()
        print("Download manager stopped.")


    def save_state(self):
        with self.lock:
            state = {
                'downloads': [item.to_dict() for item in self.downloads.values()]
                # We don't save the queue directly, state is stored in items
            }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
        except IOError as e:
            print(f"Error saving state to {STATE_FILE}: {e}")

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            print("No previous state file found.")
            return

        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)

            loaded_downloads = {}
            items_to_queue = []
            for item_data in state.get('downloads', []):
                 try:
                    item = DownloadItem.from_dict(item_data)
                    loaded_downloads[item.filename] = item
                    # Items that were paused, errored, or queued should be put back in queue on start
                    # Items marked 'downloading' were interrupted, treat as paused.
                    if item.status in ['queued', 'paused', 'error']:
                         # Let's re-queue them on start() instead of load()
                         # Update status if it was 'downloading' -> 'paused'
                         if item_data.get('status') == 'downloading':
                             item.status = 'paused' # Mark as paused on load if interrupted
                         print(f"Loaded state for {item.filename} ({item.status})")
                 except Exception as e:
                      print(f"Error loading item state: {item_data.get('url')}. Error: {e}")


            with self.lock:
                 self.downloads = loaded_downloads

            print(f"Loaded {len(self.downloads)} download states from {STATE_FILE}.")

        except (IOError, json.JSONDecodeError) as e:
            print(f"Error loading state from {STATE_FILE}: {e}. Starting fresh.")
            self.downloads = {} # Start fresh if state is corrupt

# --- Signal Handling for Graceful Exit ---
manager = None # Global manager instance

def signal_handler(sig, frame):
    print('\nCtrl+C detected. Shutting down gracefully...')
    if manager:
        manager.stop()
    sys.exit(0)

# --- Main CLI ---
if __name__ == "__main__":
    manager = DownloadManager()
    signal.signal(signal.SIGINT, signal_handler) # Register Ctrl+C handler

    manager.start() # Start the worker thread

    print("\n--- Simple Download Manager ---")
    print("Commands: add <url>, pause <filename>, resume <filename>, list, exit")

    while True:
        try:
            command = input("> ").strip().lower().split()
            if not command:
                continue

            action = command[0]

            if action == "add" and len(command) > 1:
                url = command[1]
                manager.add_download(url)
            elif action == "pause" and len(command) > 1:
                filename = " ".join(command[1:]) # Handle filenames with spaces
                manager.pause_download(filename)
            elif action == "resume" and len(command) > 1:
                filename = " ".join(command[1:]) # Handle filenames with spaces
                manager.resume_download(filename)
            elif action == "list":
                print(manager.get_status())
            elif action == "exit":
                manager.stop()
                break
            else:
                print("Unknown command. Available: add <url>, pause <filename>, resume <filename>, list, exit")

        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            # Optionally add more robust error handling or logging here
