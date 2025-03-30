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
CHUNK_SIZE = 8192
DOWNLOAD_DIR = "downloads"
STATE_FILE = "download_state.json"

# --- Download Item Class ---
class DownloadItem:
    def __init__(self, url, filename=None, total_size=0, downloaded_size=0, status='queued', error_message=None):
        self.url = url
        self.filename = filename or self._generate_filename(url)
        self.temp_filename = os.path.join(DOWNLOAD_DIR, self.filename + ".part")
        self.final_filename = os.path.join(DOWNLOAD_DIR, self.filename)
        self.progress_file = os.path.join(DOWNLOAD_DIR, self.filename + ".progress")

        self.total_size = total_size
        self.downloaded_size = downloaded_size
        # Ensure status is valid on init, default to queued if not
        valid_statuses = ['queued', 'downloading', 'paused', 'completed', 'error']
        self.status = status if status in valid_statuses else 'queued'
        self.error_message = error_message
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()

    def _generate_filename(self, url):
        try:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                return f"download_{hash(url)}.unknown"
            # Basic sanitization (replace potentially problematic chars)
            # A more robust solution might use a library like `pathvalidate`
            filename = filename.replace('/', '_').replace('\\', '_').replace(':', '_')
            return filename
        except Exception:
            return f"download_{hash(url)}.unknown"

    def to_dict(self):
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
        item = cls(
            url=data['url'],
            filename=data.get('filename'),
            total_size=data.get('total_size', 0),
            downloaded_size=data.get('downloaded_size', 0),
            status=data.get('status', 'queued'),
            error_message=data.get('error_message')
        )
        # Simplified state restoration logic from previous version
        if item.status in ['paused', 'error', 'downloading']:
             if os.path.exists(item.progress_file):
                 try:
                     with open(item.progress_file, 'r') as pf:
                         item.downloaded_size = int(pf.read().strip())
                     if item.status == 'downloading': # Treat interrupted as paused
                         item.status = 'paused'
                 except (IOError, ValueError):
                     # Reset if progress file is bad
                     item.downloaded_size = 0
                     item.status = 'queued'
                     if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                     if os.path.exists(item.progress_file): os.remove(item.progress_file)
             elif item.status != 'queued': # If progress missing but shouldn't be
                 item.downloaded_size = 0
                 item.status = 'queued'
                 if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
        elif item.status == 'completed':
             if not os.path.exists(item.final_filename):
                 item.status = 'queued'
                 item.downloaded_size = 0

        return item

    def __str__(self):
        # Simplified string representation - TUI will handle progress bars
        state_upper = self.status.upper()
        if self.status == 'error':
            err_msg = f" - {self.error_message}" if self.error_message else ""
            return f"[{self.filename}] {state_upper}{err_msg}"
        elif self.status == 'completed':
             size_mb = f"{self.total_size / (1024*1024):.2f} MB" if self.total_size else "Size unknown"
             return f"[{self.filename}] {state_upper} ({size_mb})"
        elif self.status == 'paused':
             percent = (self.downloaded_size / self.total_size) * 100 if self.total_size else 0
             size_mb = f"{self.downloaded_size / (1024*1024):.2f} MB"
             return f"[{self.filename}] {state_upper} at {percent:.1f}% ({size_mb})"
        else: # queued, downloading (base string)
             return f"[{self.filename}] {state_upper}"


# --- Download Manager Class ---
class DownloadManager:
    def __init__(self):
        self.download_queue = queue.Queue()
        self.downloads = {} # {filename: DownloadItem}
        self.lock = threading.Lock()
        self.worker_thread = None
        self.stop_event = threading.Event()

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.load_state()

    def get_items(self) -> list[DownloadItem]:
        """ Returns a thread-safe copy of the download items list. """
        with self.lock:
            # Return a copy to avoid race conditions during iteration in the UI
            return list(self.downloads.values())

    def add_download(self, url):
        item = DownloadItem(url)
        # Prevent adding duplicates by filename
        with self.lock:
            if item.filename in self.downloads:
                # Maybe update the URL if requested? For now, just report.
                print(f"Download for '{item.filename}' already exists or filename conflict.")
                return False # Indicate failure/prevention

            self.downloads[item.filename] = item

        self.download_queue.put(item)
        print(f"Added '{item.filename}' to the queue.")
        self.save_state()
        return True # Indicate success

    def pause_download(self, filename):
        item_paused = False
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'downloading':
                item.pause_event.set()
                # Status update ('paused') is done by the worker thread
                item_paused = True
            # Handle other cases (already paused, not found, wrong state)
            elif item and item.status == 'paused':
                print(f"'{filename}' is already paused.")
            elif item:
                 print(f"Cannot pause '{filename}' (status: {item.status}).")
            else:
                print(f"Download '{filename}' not found.")
        if item_paused:
             print(f"Signalling pause for '{filename}'...")
        return item_paused # Return success/failure

    def resume_download(self, filename):
        item_resumed = False
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'paused':
                item.status = 'queued'
                item.pause_event.clear()
                # Important: Clear stop event too if it was set during pause/stop
                item.stop_event.clear()
                self.download_queue.put(item) # Add back to the queue
                item_resumed = True
            # Handle other cases
            elif item and item.status == 'queued':
                 print(f"'{filename}' is already queued.")
            elif item and item.status == 'downloading':
                 print(f"'{filename}' is already downloading.")
            elif item:
                 print(f"Cannot resume '{filename}' (status: {item.status}).")
            else:
                print(f"Download '{filename}' not found.")

        if item_resumed:
            print(f"Resuming '{filename}'...")
            self.save_state() # Save status change
        return item_resumed

    def pause_all(self):
        """ Signals all currently downloading items to pause. """
        paused_count = 0
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'downloading':
                    item.pause_event.set()
                    paused_count += 1
        print(f"Signalling pause for {paused_count} active download(s)...")
        return paused_count > 0

    def resume_all(self):
        """ Resumes all currently paused items by re-queuing them. """
        resumed_count = 0
        items_to_queue = []
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'paused':
                    item.status = 'queued'
                    item.pause_event.clear()
                    item.stop_event.clear() # Ensure stop is cleared
                    items_to_queue.append(item)
                    resumed_count += 1

        # Add items to queue outside the lock to avoid potential deadlocks
        # if queue operations block under rare circumstances.
        for item in items_to_queue:
             self.download_queue.put(item)

        if resumed_count > 0:
             print(f"Resuming {resumed_count} paused download(s)...")
             self.save_state() # Save status changes
        else:
             print("No paused downloads to resume.")
        return resumed_count > 0

    def _worker(self):
        print("Download worker started.")
        while not self.stop_event.is_set():
            try:
                item = self.download_queue.get(timeout=1)
            except queue.Empty:
                continue

            if self.stop_event.is_set(): break
            if item.stop_event.is_set():
                print(f"Skipping cancelled download: {item.filename}")
                self.download_queue.task_done()
                continue
            if item.status == 'completed':
                # print(f"Skipping already completed download: {item.filename}") # Can be noisy
                self.download_queue.task_done()
                continue
            # Ensure item is marked as queued if it wasn't (e.g., retrying error)
            if item.status != 'queued':
                 with self.lock: item.status = 'queued'


            print(f"Starting download: {item.filename}")
            self._process_download(item)
            self.download_queue.task_done()
            # Save state only after task is done (success, pause, error)
            self.save_state()

        print("Download worker stopped.")

    def _process_download(self, item: DownloadItem):
        # --- Reset state for this attempt ---
        with self.lock:
             item.status = 'downloading'
             item.error_message = None
             item.pause_event.clear()
             # Do not clear stop_event here, it might be set globally

        headers = {}
        current_size = 0
        file_mode = 'wb' # Default to write, change to append if resuming

        # --- Check for existing progress/resume ---
        if os.path.exists(item.progress_file):
            try:
                with open(item.progress_file, 'r') as pf:
                    saved_size = int(pf.read().strip())
                # Sanity check: ensure partial file size matches progress file
                # This handles cases where the .part file was truncated or deleted externally
                actual_part_size = 0
                if os.path.exists(item.temp_filename):
                    actual_part_size = os.path.getsize(item.temp_filename)

                if actual_part_size == saved_size and saved_size > 0:
                    current_size = saved_size
                    item.downloaded_size = current_size
                    headers['Range'] = f'bytes={current_size}-'
                    file_mode = 'ab' # Append mode
                    print(f"Resuming {item.filename} from {current_size} bytes.")
                elif saved_size > 0:
                    print(f"Warning: Progress file size ({saved_size}) mismatches partial file size ({actual_part_size}) for {item.filename}. Restarting download.")
                    current_size = 0
                    item.downloaded_size = 0
                    # Clean up inconsistent state
                    if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                    if os.path.exists(item.progress_file): os.remove(item.progress_file)

            except (IOError, ValueError) as e:
                print(f"Warning: Error reading progress file for {item.filename} ({e}). Restarting download.")
                current_size = 0
                item.downloaded_size = 0
                if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                if os.path.exists(item.progress_file): os.remove(item.progress_file)

        # --- Perform Download ---
        session = requests.Session() # Use session for potential keep-alive
        response = None
        try:
            response = session.get(item.url, headers=headers, stream=True, timeout=30)
            response.raise_for_status()

            # --- Handle Resume Response ---
            is_resuming = False
            if current_size > 0 and response.status_code == 206: # Partial Content
                is_resuming = True
                content_range = response.headers.get('Content-Range')
                if content_range:
                    try: item.total_size = int(content_range.split('/')[-1])
                    except (ValueError, IndexError): pass # Ignore malformed header
                print(f"Server confirmed resume for {item.filename}.")
            elif current_size > 0: # Requested resume, but got 200 OK or other
                print(f"Server didn't support resume (Status: {response.status_code}). Restarting {item.filename}.")
                current_size = 0
                item.downloaded_size = 0
                file_mode = 'wb' # Overwrite
                if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                if os.path.exists(item.progress_file): os.remove(item.progress_file)

            # --- Get Total Size (if not known or resuming didn't provide) ---
            if item.total_size <= 0:
                content_length = response.headers.get('content-length')
                if content_length:
                    try:
                        # If resuming, total size is current + remaining content_length
                        item.total_size = current_size + int(content_length)
                    except ValueError: pass # Ignore invalid content-length
                # else: total size remains unknown (0)

            # --- Download Loop ---
            last_progress_save_time = time.time()
            # Ensure directory exists right before opening file
            os.makedirs(os.path.dirname(item.temp_filename), exist_ok=True)

            with open(item.temp_filename, file_mode) as f:
                # Ensure file pointer is correct for append mode
                if file_mode == 'ab':
                    f.seek(current_size)

                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk: continue # filter keep-alive chunks

                    # --- Check for Pause/Stop ---
                    should_stop = False
                    if item.pause_event.is_set():
                        print(f"\nPausing download via event: {item.filename}")
                        with self.lock: item.status = 'paused'
                        should_stop = True
                    if item.stop_event.is_set():
                        print(f"\nStopping download via item event: {item.filename}")
                        with self.lock: item.status = 'paused' # Treat stop as pause for resume
                        should_stop = True
                    if self.stop_event.is_set():
                        print(f"\nStopping download via global event: {item.filename}")
                        with self.lock: item.status = 'paused'
                        should_stop = True

                    if should_stop:
                        # Save progress before stopping
                        try:
                            # Flush buffer before getting size? Maybe not needed.
                            f.flush()
                            os.fsync(f.fileno()) # Ensure data written to disk
                            # Use f.tell() for potentially more accurate current position
                            final_size = f.tell()
                            item.downloaded_size = final_size # Update item state
                            with open(item.progress_file, 'w') as pf:
                                pf.write(str(final_size))
                            print(f"Saved progress ({final_size} bytes) for {item.filename} before stopping.")
                        except IOError as e:
                             print(f"\nError saving progress for {item.filename} on stop: {e}")
                        return # Exit _process_download for this item

                    # --- Write Chunk & Update Progress ---
                    f.write(chunk)
                    item.downloaded_size += len(chunk) # This is faster than f.tell() in loop

                    # --- Save progress periodically ---
                    current_time = time.time()
                    if current_time - last_progress_save_time > 5: # Save every 5 seconds
                        try:
                            # Flush before saving progress? Might impact performance.
                            # Save based on tracked size, not f.tell()
                            current_tracked_size = item.downloaded_size
                            with open(item.progress_file, 'w') as pf:
                                pf.write(str(current_tracked_size))
                            last_progress_save_time = current_time
                        except IOError as e:
                            print(f"\nError saving periodic progress for {item.filename}: {e}")
                            # Decide whether to abort on persistent save errors?

            # --- Download Complete ---
            print(f"\nDownload stream finished for {item.filename}.")
            # Final check: ensure file size matches expected total size
            final_downloaded_size = os.path.getsize(item.temp_filename)
            item.downloaded_size = final_downloaded_size # Update with actual size

            if item.total_size > 0 and final_downloaded_size != item.total_size:
                raise IOError(f"Download incomplete: Expected {item.total_size} bytes, got {final_downloaded_size}")
            elif item.total_size == 0: # Size was unknown, update total_size now
                 item.total_size = final_downloaded_size

            # Save final progress state just before rename (redundant?)
            # try:
            #      with open(item.progress_file, 'w') as pf: pf.write(str(item.downloaded_size))
            # except IOError: pass # Ignore if this fails, main thing is rename

            # Rename temp file to final file
            os.rename(item.temp_filename, item.final_filename)

            # Clean up progress file
            if os.path.exists(item.progress_file):
                os.remove(item.progress_file)

            with self.lock:
                item.status = 'completed'
            print(f"Download completed and verified: {item.filename}")

        except requests.exceptions.RequestException as e:
            print(f"\nDownload Error ({item.filename}): {e}")
            with self.lock:
                item.status = 'error'
                item.error_message = str(e)
            # Save progress on network errors for potential resume
            if item.downloaded_size > 0:
                try:
                    with open(item.progress_file, 'w') as pf: pf.write(str(item.downloaded_size))
                except IOError as ioe: print(f"Could not save progress during error for {item.filename}: {ioe}")

        except IOError as e:
            print(f"\nFile I/O Error ({item.filename}): {e}")
            with self.lock:
                item.status = 'error'
                item.error_message = f"File I/O Error: {e}"
            # Don't save progress on file write errors typically

        except Exception as e:
            print(f"\nUnexpected Error ({item.filename}): {e}")
            # Log the full traceback for unexpected errors
            import traceback
            traceback.print_exc()
            with self.lock:
                item.status = 'error'
                item.error_message = f"Unexpected Error: {e}"
            # Attempt to save progress
            if item.downloaded_size > 0:
                 try:
                      with open(item.progress_file, 'w') as pf: pf.write(str(item.downloaded_size))
                 except IOError as ioe: print(f"Could not save progress during error for {item.filename}: {ioe}")

        finally:
            # Ensure response is closed if it was opened
            if response:
                response.close()
            # Ensure session is closed? Typically not needed per request.
            # session.close()

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            print("Worker thread already running.")
            return

        self.stop_event.clear()
        items_to_queue_on_start = []
        with self.lock:
            for item in self.downloads.values():
                # If state is paused, error, or was downloading (interrupted) -> queue it
                if item.status in ['paused', 'error']:
                    item.status = 'queued' # Mark for queueing
                    item.pause_event.clear()
                    item.stop_event.clear()
                    items_to_queue_on_start.append(item)
                # No need to explicitly handle 'downloading' as from_dict converts it to 'paused'

        # Add to queue outside lock
        for item in items_to_queue_on_start:
             self.download_queue.put(item)
        if items_to_queue_on_start:
             print(f"Queued {len(items_to_queue_on_start)} pending downloads on start.")
             self.save_state() # Save the status change to 'queued'

        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def stop(self, graceful=True):
        print("Stopping download manager...")
        if not self.worker_thread or not self.worker_thread.is_alive():
            print("Worker thread not running.")
            # Still save state even if worker wasn't running
            self.save_state()
            return

        self.stop_event.set() # Signal global stop

        # Signal currently downloading item to stop/pause
        active_item_signalled = False
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'downloading':
                    print(f"Signalling active download '{item.filename}' to stop...")
                    item.stop_event.set() # Signal specific item
                    active_item_signalled = True
                    # Don't break, signal all active ones if multi-download is ever added

        if graceful and active_item_signalled:
            print("Waiting for worker thread to finish current task gracefully...")
            # Give worker time to save progress and exit loop iteration
            self.worker_thread.join(timeout=10) # Wait up to 10 seconds
            if self.worker_thread.is_alive():
                 print("Worker thread did not stop gracefully in time.")
            else:
                 print("Worker thread stopped.")
        elif graceful:
             # If no active download, worker should stop quickly
             self.worker_thread.join(timeout=2)


        # Ensure final state is saved
        print("Saving final state...")
        self.save_state() # Save state after stopping attempt
        print("Download manager stop sequence complete.")


    def save_state(self):
        with self.lock:
            # Create a snapshot of the state within the lock
            state = {
                'downloads': [item.to_dict() for item in self.downloads.values()]
            }
        try:
            # Write state outside the lock
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
            for item_data in state.get('downloads', []):
                 try:
                    item = DownloadItem.from_dict(item_data)
                    # Avoid overwriting if filename collision happens during load?
                    # Or just let the last one win.
                    loaded_downloads[item.filename] = item
                 except Exception as e:
                      print(f"Error loading item state for {item_data.get('url')}. Error: {e}")

            with self.lock:
                 self.downloads = loaded_downloads

            print(f"Loaded {len(self.downloads)} download states from {STATE_FILE}.")

        except (IOError, json.JSONDecodeError) as e:
            print(f"Error loading state from {STATE_FILE}: {e}. Starting fresh.")
            self.downloads = {} # Start fresh if state is corrupt
