import requests
import os
import threading
import queue
import time
import json
import signal
import sys
from urllib.parse import urlparse
import logging # Import logging

# --- Configuration ---
CHUNK_SIZE = 8192
DOWNLOAD_DIR = "downloads"
STATE_FILE = "download_state.json"
LOG_FILE = "downloader.log" # Optional log file

# --- Setup Logging (Optional, but recommended over print) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s',
    filename=LOG_FILE,
    filemode='a' # Append to the log file
)
# You might want to disable logging propagation to root logger if it outputs to console
# logging.getLogger().propagate = False


# --- Download Item Class (No changes needed here) ---
class DownloadItem:
    # ... (no print statements here usually) ...
    def __init__(self, url, filename=None, total_size=0, downloaded_size=0, status='queued', error_message=None):
        self.url = url
        self.filename = filename or self._generate_filename(url)
        self.temp_filename = os.path.join(DOWNLOAD_DIR, self.filename + ".part")
        self.final_filename = os.path.join(DOWNLOAD_DIR, self.filename)
        self.progress_file = os.path.join(DOWNLOAD_DIR, self.filename + ".progress")

        self.total_size = total_size
        self.downloaded_size = downloaded_size
        valid_statuses = ['queued', 'downloading', 'paused', 'completed', 'error']
        self.status = status if status in valid_statuses else 'queued'
        self.error_message = error_message
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()

        self.start_time: Optional[float] = None # Time when download activity last started/resumed
        self.last_speed_calc_time: float = 0.0  # Time of the last speed calculation
        self.bytes_at_last_calc: int = 0      # downloaded_size at the last speed calculation
        self.current_speed: float = 0.0       # Bytes per second
        self.eta_seconds: Optional[float] = None  # Estimated seconds remaining

    def _generate_filename(self, url):
        try:
            parsed_url = urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                filename = f"download_{hash(url)}.unknown"
            # Basic sanitization
            filename = filename.replace('/', '_').replace('\\', '_').replace(':', '_').replace('?', '_').replace('*', '_')
            return filename
        except Exception as e:
            logging.error(f"Error generating filename for {url}: {e}")
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
        if item.status in ['paused', 'error', 'downloading']:
             if os.path.exists(item.progress_file):
                 try:
                     with open(item.progress_file, 'r') as pf:
                         item.downloaded_size = int(pf.read().strip())
                     if item.status == 'downloading':
                         item.status = 'paused'
                 except (IOError, ValueError):
                     # logging.warning(f"Bad progress file for {item.filename}, resetting.")
                     item.downloaded_size = 0
                     item.status = 'queued'
                     if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                     if os.path.exists(item.progress_file): os.remove(item.progress_file)
             elif item.status != 'queued':
                 # logging.warning(f"Progress file missing for {item.filename}, resetting.")
                 item.downloaded_size = 0
                 item.status = 'queued'
                 if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
        elif item.status == 'completed':
             if not os.path.exists(item.final_filename):
                 # logging.warning(f"Completed file missing for {item.filename}, resetting.")
                 item.status = 'queued'
                 item.downloaded_size = 0
        return item

    def __str__(self):
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
        self.downloads = {}
        self.lock = threading.Lock()
        self.worker_thread = None
        self.stop_event = threading.Event()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.load_state()

    def get_items(self) -> list[DownloadItem]:
        with self.lock:
            return list(self.downloads.values())

    def add_download(self, url):
        item = DownloadItem(url)
        with self.lock:
            if item.filename in self.downloads:
                logging.warning(f"Download for '{item.filename}' already exists.")
                # print(f"Download for '{item.filename}' already exists or filename conflict.") # REMOVED
                return False
            self.downloads[item.filename] = item
        self.download_queue.put(item)
        logging.info(f"Added '{item.filename}' to queue (URL: {url}).")
        # print(f"Added '{item.filename}' to the queue.") # REMOVED
        self.save_state()
        return True

    def pause_download(self, filename):
        item_paused = False
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'downloading':
                item.pause_event.set()
                item_paused = True
            # REMOVED print statements for other conditions - TUI shows status
            # elif item and item.status == 'paused':
            #     print(f"'{filename}' is already paused.")
            # elif item:
            #      print(f"Cannot pause '{filename}' (status: {item.status}).")
            # else:
            #     print(f"Download '{filename}' not found.")
        # if item_paused:
        #      print(f"Signalling pause for '{filename}'...") # REMOVED
        logging.info(f"Pause requested for {filename}. Success: {item_paused}")
        return item_paused

    def resume_download(self, filename):
        item_resumed = False
        with self.lock:
            item = self.downloads.get(filename)
            if item and item.status == 'paused':
                item.status = 'queued'
                item.pause_event.clear()
                item.stop_event.clear()
                self.download_queue.put(item)
                item_resumed = True
            # REMOVED print statements for other conditions
            # elif item and item.status == 'queued':
            #      print(f"'{filename}' is already queued.")
            # ... etc ...

        if item_resumed:
            # print(f"Resuming '{filename}'...") # REMOVED
            self.save_state()
        logging.info(f"Resume requested for {filename}. Success: {item_resumed}")
        return item_resumed

    def pause_all(self):
        paused_count = 0
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'downloading':
                    item.pause_event.set()
                    paused_count += 1
        # print(f"Signalling pause for {paused_count} active download(s)...") # REMOVED
        logging.info(f"Pause All requested. Signalled {paused_count} items.")
        return paused_count > 0

    def resume_all(self):
        resumed_count = 0
        items_to_queue = []
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'paused':
                    item.status = 'queued'
                    item.pause_event.clear()
                    item.stop_event.clear()
                    items_to_queue.append(item)
                    resumed_count += 1

        for item in items_to_queue:
             self.download_queue.put(item)

        if resumed_count > 0:
             # print(f"Resuming {resumed_count} paused download(s)...") # REMOVED
             self.save_state()
        # else:
        #      print("No paused downloads to resume.") # REMOVED
        logging.info(f"Resume All requested. Resumed {resumed_count} items.")
        return resumed_count > 0

    def _worker(self):
        logging.info("Download worker started.")
        while not self.stop_event.is_set():
            try:
                item = self.download_queue.get(timeout=1)
            except queue.Empty:
                continue

            if self.stop_event.is_set(): break
            if item.stop_event.is_set():
                logging.info(f"Skipping cancelled download: {item.filename}")
                # print(f"Skipping cancelled download: {item.filename}") # REMOVED
                self.download_queue.task_done()
                continue
            if item.status == 'completed':
                self.download_queue.task_done()
                continue
            if item.status != 'queued':
                 with self.lock: item.status = 'queued' # Ensure correct state

            logging.info(f"Starting download: {item.filename}")
            # print(f"Starting download: {item.filename}") # REMOVED
            self._process_download(item)
            self.download_queue.task_done()
            self.save_state()

        logging.info("Download worker stopped.")

    def _process_download(self, item: DownloadItem):
        with self.lock:
             item.status = 'downloading'
             item.error_message = None
             item.current_speed = 0.0
             item.eta_seconds = None
             item.start_time = None
             item.pause_event.clear()

        headers = {}
        current_size = 0
        file_mode = 'wb'
        session = requests.Session()
        response = None
        speed_calc_interval = 1.0 # Recalculate speed every second

        try:
            # --- Resume Logic ---
            if os.path.exists(item.progress_file):
                try:
                    with open(item.progress_file, 'r') as pf: saved_size = int(pf.read().strip())
                    actual_part_size = os.path.getsize(item.temp_filename) if os.path.exists(item.temp_filename) else 0

                    if actual_part_size == saved_size and saved_size > 0:
                        current_size = saved_size
                        item.downloaded_size = current_size
                        headers['Range'] = f'bytes={current_size}-'
                        file_mode = 'ab'
                        logging.info(f"Resuming {item.filename} from {current_size} bytes.")
                        # print(f"Resuming {item.filename} from {current_size} bytes.") # REMOVED
                    elif saved_size > 0:
                        logging.warning(f"Progress/part mismatch for {item.filename}. Restarting.")
                        # print(f"Warning: Progress file size ... Restarting download.") # REMOVED
                        current_size = 0; item.downloaded_size = 0
                        if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                        if os.path.exists(item.progress_file): os.remove(item.progress_file)
                except (IOError, ValueError) as e:
                    logging.warning(f"Error reading progress file for {item.filename} ({e}). Restarting.")
                    # print(f"Warning: Error reading progress file ... Restarting download.") # REMOVED
                    current_size = 0; item.downloaded_size = 0
                    if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                    if os.path.exists(item.progress_file): os.remove(item.progress_file)

            # --- Get Request ---
            response = session.get(item.url, headers=headers, stream=True, timeout=60) # Increased timeout
            response.raise_for_status()

            # --- Handle Resume Response ---
            is_resuming = False
            if current_size > 0 and response.status_code == 206:
                is_resuming = True
                logging.info(f"Server confirmed resume for {item.filename}.")
                # print(f"Server confirmed resume for {item.filename}.") # REMOVED - This one specifically was in the screenshot!
                content_range = response.headers.get('Content-Range')
                if content_range:
                    try: item.total_size = int(content_range.split('/')[-1])
                    except (ValueError, IndexError): pass
            elif current_size > 0:
                logging.warning(f"Server didn't support resume (Status: {response.status_code}). Restarting {item.filename}.")
                # print(f"Server didn't support resume ... Restarting {item.filename}.") # REMOVED
                current_size = 0; item.downloaded_size = 0; file_mode = 'wb'
                if os.path.exists(item.temp_filename): os.remove(item.temp_filename)
                if os.path.exists(item.progress_file): os.remove(item.progress_file)

            # --- Get Total Size ---
            if item.total_size <= 0:
                content_length = response.headers.get('content-length')
                if content_length:
                    try: item.total_size = current_size + int(content_length)
                    except ValueError: pass
                else: # Size unknown
                    logging.warning(f"Content-Length header missing for {item.filename}")

            # --- Download Loop ---
            last_progress_save_time = time.time()
            os.makedirs(os.path.dirname(item.temp_filename), exist_ok=True)

            # ** Initialize speed tracking when download starts/resumes **
            initial_byte_count = item.downloaded_size
            item.start_time = time.time()
            item.last_speed_calc_time = item.start_time
            item.bytes_at_last_calc = initial_byte_count

            with open(item.temp_filename, file_mode) as f:
                if file_mode == 'ab': f.seek(current_size)

                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk: continue
                    
                    current_loop_time = time.time() # Use consistent time for calcs in loop

                    # --- Check for Pause/Stop ---
                    should_stop = False
                    if item.pause_event.is_set():
                        logging.info(f"Pausing download via event: {item.filename}")
                        # print(f"\nPausing download via event: {item.filename}") # REMOVED
                        with self.lock: item.status = 'paused'
                        should_stop = True
                    # Check global stop first potentially
                    if self.stop_event.is_set():
                        logging.info(f"Stopping download via global event: {item.filename}")
                        # print(f"\nStopping download via global event: {item.filename}") # REMOVED
                        with self.lock: item.status = 'paused'
                        should_stop = True
                    elif item.stop_event.is_set(): # Check item stop if global not set
                         logging.info(f"Stopping download via item event: {item.filename}")
                         # print(f"\nStopping download via item event: {item.filename}") # REMOVED
                         with self.lock: item.status = 'paused'
                         should_stop = True

                    if should_stop:
                        try:
                            f.flush(); os.fsync(f.fileno())
                            final_size = f.tell()
                            item.downloaded_size = final_size
                            with open(item.progress_file, 'w') as pf: pf.write(str(final_size))
                            logging.info(f"Saved progress ({final_size} bytes) for {item.filename} before stopping.")
                            
                            ## Reset speed/ETA on pause/stop
                            item.current_speed = 0.0
                            item.eta_seconds = None 
                        except IOError as e:
                             logging.error(f"Error saving progress for {item.filename} on stop: {e}")
                             # print(f"\nError saving progress for {item.filename} on stop: {e}") # REMOVED
                        return # Exit _process_download

                    # --- Write Chunk & Update Progress ---
                    chunk_len = len(chunk)
                    f.write(chunk)
                    item.downloaded_size += chunk_len

                    # --- Calculate Speed & ETA ---
                    if current_loop_time - item.last_speed_calc_time >= speed_calc_interval:
                        time_delta = current_loop_time - item.last_speed_calc_time
                        bytes_delta = item.downloaded_size - item.bytes_at_last_calc

                        if time_delta > 0:
                            item.current_speed = bytes_delta / time_delta
                        else:
                            item.current_speed = 0.0 # Avoid division by zero

                        # Calculate ETA
                        if item.total_size > 0 and item.current_speed > 1: # Avoid near-zero speed ETA spikes
                            remaining_bytes = item.total_size - item.downloaded_size
                            if remaining_bytes > 0:
                                item.eta_seconds = remaining_bytes / item.current_speed
                            else:
                                item.eta_seconds = 0 # Already finished technically
                        else:
                            item.eta_seconds = None # Cannot estimate

                        # Update trackers for next calculation
                        item.last_speed_calc_time = current_loop_time
                        item.bytes_at_last_calc = item.downloaded_size

                    # --- Save progress periodically ---
                    current_time = time.time()
                    if current_time - last_progress_save_time > 5:
                        try:
                            current_tracked_size = item.downloaded_size
                            with open(item.progress_file, 'w') as pf: pf.write(str(current_tracked_size))
                            last_progress_save_time = current_time
                        except IOError as e:
                            logging.error(f"Error saving periodic progress for {item.filename}: {e}")
                            # print(f"\nError saving periodic progress for {item.filename}: {e}") # REMOVED

            # --- Download Complete ---
            logging.info(f"Download stream finished for {item.filename}.")
            final_downloaded_size = os.path.getsize(item.temp_filename)
            item.downloaded_size = final_downloaded_size
            item.current_speed = 0.0 # Reset speed/ETA on completion
            item.eta_seconds = None

            if item.total_size > 0 and final_downloaded_size != item.total_size:
                raise IOError(f"Incomplete: Expected {item.total_size}, got {final_downloaded_size}")
            elif item.total_size == 0:
                 item.total_size = final_downloaded_size

            os.rename(item.temp_filename, item.final_filename)
            if os.path.exists(item.progress_file): os.remove(item.progress_file)

            with self.lock: item.status = 'completed'
            logging.info(f"Download completed and verified: {item.filename}")
            # print(f"Download completed and verified: {item.filename}") # REMOVED

        except requests.exceptions.RequestException as e:
            logging.error(f"Download Error ({item.filename}): {e}")
            # print(f"\nDownload Error ({item.filename}): {e}") # REMOVED
            with self.lock: item.status = 'error'; item.error_message = str(e)
            
            # Reset speed and ETA on error
            item.current_speed = 0.0; 
            item.eta_seconds = None

            if item.downloaded_size > 0: # Save progress on network errors
                try:
                    with open(item.progress_file, 'w') as pf: pf.write(str(item.downloaded_size))
                except IOError as ioe: logging.error(f"Could not save progress during error ({item.filename}): {ioe}")
        except IOError as e:
            logging.error(f"File I/O Error ({item.filename}): {e}")
            # print(f"\nFile I/O Error ({item.filename}): {e}") # REMOVED
            with self.lock: item.status = 'error'; item.error_message = f"File I/O Error: {e}"

            # Reset speed and ETA on error
            item.current_speed = 0.0; 
            item.eta_seconds = None

        except Exception as e:
            logging.exception(f"Unexpected Error ({item.filename}): {e}") # Log full traceback
            
            # Reset speed and ETA on error
            item.current_speed = 0.0; 
            item.eta_seconds = None

            with self.lock: item.status = 'error'; item.error_message = f"Unexpected Error: {e}"
            if item.downloaded_size > 0: # Try save progress
                 try:
                      with open(item.progress_file, 'w') as pf: pf.write(str(item.downloaded_size))
                 except IOError as ioe: logging.error(f"Could not save progress during error ({item.filename}): {ioe}")

        finally:
            if response: response.close()

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            logging.warning("Start called but worker thread already running.")
            # print("Worker thread already running.") # REMOVED
            return

        logging.info("Starting Download Manager...")
        self.stop_event.clear()
        items_to_queue_on_start = []
        with self.lock:
            for item in self.downloads.values():
                if item.status in ['paused', 'error']:
                    item.status = 'queued'
                    item.pause_event.clear()
                    item.stop_event.clear()
                    items_to_queue_on_start.append(item)

        for item in items_to_queue_on_start:
             self.download_queue.put(item)
        if items_to_queue_on_start:
             logging.info(f"Queued {len(items_to_queue_on_start)} pending downloads on start.")
             # print(f"Queued {len(items_to_queue_on_start)} pending downloads on start.") # REMOVED
             self.save_state()

        self.worker_thread = threading.Thread(target=self._worker, name="DownloadWorker", daemon=True) # Added name
        self.worker_thread.start()

    def stop(self, graceful=True):
        logging.info(f"Stopping download manager (graceful={graceful})...")
        # print("Stopping download manager...") # REMOVED
        if not self.worker_thread or not self.worker_thread.is_alive():
            logging.warning("Stop called but worker thread not running.")
            # print("Worker thread not running.") # REMOVED
            self.save_state()
            return

        self.stop_event.set()
        active_item_signalled = False
        with self.lock:
            for item in self.downloads.values():
                if item.status == 'downloading':
                    logging.info(f"Signalling active download '{item.filename}' to stop...")
                    # print(f"Signalling active download '{item.filename}' to stop...") # REMOVED
                    item.stop_event.set()
                    active_item_signalled = True

        if graceful and (active_item_signalled or not self.download_queue.empty()): # Also wait if queue isn't empty? No, worker exit depends on active task finishing
            logging.info("Waiting for worker thread to finish current task gracefully...")
            # print("Waiting for worker thread to finish current task gracefully...") # REMOVED
            self.worker_thread.join(timeout=10)
            if self.worker_thread.is_alive():
                 logging.warning("Worker thread did not stop gracefully in time.")
                 # print("Worker thread did not stop gracefully in time.") # REMOVED
            else:
                 logging.info("Worker thread stopped.")
                 # print("Worker thread stopped.") # REMOVED
        elif graceful:
             self.worker_thread.join(timeout=2) # Shorter timeout if no active task

        logging.info("Saving final state...")
        # print("Saving final state...") # REMOVED
        self.save_state()
        logging.info("Download manager stop sequence complete.")
        # print("Download manager stop sequence complete.") # REMOVED

    def save_state(self):
        with self.lock:
            state = {'downloads': [item.to_dict() for item in self.downloads.values()]}
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
        except IOError as e:
            logging.error(f"Error saving state to {STATE_FILE}: {e}")
            # print(f"Error saving state to {STATE_FILE}: {e}") # REMOVED

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            logging.info("No previous state file found.")
            # print("No previous state file found.") # REMOVED
            return
        logging.info(f"Loading state from {STATE_FILE}")
        try:
            with open(STATE_FILE, 'r') as f: state = json.load(f)
            loaded_downloads = {}
            for item_data in state.get('downloads', []):
                 try:
                    item = DownloadItem.from_dict(item_data)
                    loaded_downloads[item.filename] = item
                 except Exception as e:
                      logging.error(f"Error loading item state for {item_data.get('url')}. Error: {e}")

            with self.lock: self.downloads = loaded_downloads
            logging.info(f"Loaded {len(self.downloads)} download states.")
            # print(f"Loaded {len(self.downloads)} download states from {STATE_FILE}.") # REMOVED
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Error loading state from {STATE_FILE}: {e}. Starting fresh.")
            # print(f"Error loading state from {STATE_FILE}: {e}. Starting fresh.") # REMOVED
            self.downloads = {}
