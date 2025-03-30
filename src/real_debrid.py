import requests
import os
import logging
from typing import List, Optional
from dataclasses import dataclass
from dotenv import load_dotenv

# Configure logging for this module (optional, but good practice)
log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler()) # Avoids 'No handler found' warnings if not configured by main app

# --- Constants ---
API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
DOWNLOADS_ENDPOINT = "/downloads"
ENV_VAR_NAME = "REAL_DEBRID_TOKEN"

# --- Data Structure for Downloads ---
@dataclass
class RealDebridDownload:
    """ Represents a downloadable item from Real-Debrid. """
    id: str
    filename: str
    filesize: int
    download_url: str # The actual URL to download the file content
    link: str # The RD link page (less useful for direct download)
    host: str

# --- Custom Exceptions ---
class RealDebridError(Exception):
    """ Base exception for Real-Debrid client errors. """
    pass

class TokenError(RealDebridError):
    """ Exception raised when the API token is missing or invalid. """
    pass

# --- Real-Debrid Client ---
class RealDebridClient:
    """
    Client to interact with the Real-Debrid API.
    Requires the 'REAL_DEBRID_TOKEN' environment variable to be set.
    """
    def __init__(self):
        self.token = os.getenv(ENV_VAR_NAME)
        if not self.token:
            log.error(f"Environment variable {ENV_VAR_NAME} is not set.")
            raise TokenError(f"Real-Debrid API token not found in environment variable '{ENV_VAR_NAME}'.")
        self.base_url = API_BASE_URL
        self._headers = {
            "Authorization": f"Bearer {self.token}"
        }
        log.info("RealDebridClient initialized.")

    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """ Helper method to make authenticated requests. """
        url = self.base_url + endpoint
        log.debug(f"Making RD request: {method} {url}")
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers,
                timeout=15, # Add a reasonable timeout
                **kwargs
            )
            # Raise HTTPError for bad responses (4xx or 5xx)
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as e:
             log.error(f"Request timed out: {method} {url} - {e}")
             raise RealDebridError(f"Request timed out: {e}") from e
        except requests.exceptions.RequestException as e:
            log.error(f"Request failed: {method} {url} - {e}")
            status_code = e.response.status_code if e.response is not None else "N/A"
            # Check specifically for 401 Unauthorized, often means bad token
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                 raise TokenError(f"Authentication failed (401). Check your Real-Debrid token.") from e
            raise RealDebridError(f"API request failed (Status: {status_code}): {e}") from e

    def get_downloads(self) -> List[RealDebridDownload]:
        """
        Fetches the list of available downloads from the /downloads endpoint.

        Returns:
            A list of RealDebridDownload objects.

        Raises:
            RealDebridError: If the API request fails or returns unexpected data.
            TokenError: If authentication fails (e.g., bad token).
        """
        log.info("Fetching downloads from Real-Debrid...")
        try:
            response = self._make_request("GET", DOWNLOADS_ENDPOINT)
            data = response.json()

            if not isinstance(data, list):
                log.error(f"Unexpected API response format: Expected list, got {type(data)}")
                raise RealDebridError("Unexpected API response format. Expected a list.")

            downloads: List[RealDebridDownload] = []
            for item in data:
                if not isinstance(item, dict):
                    log.warning(f"Skipping non-dictionary item in downloads list: {item}")
                    continue
                try:
                    # Extract only the necessary fields
                    download_item = RealDebridDownload(
                        id=item['id'],
                        filename=item['filename'],
                        filesize=item['filesize'],
                        # Critical: Use the 'download' field for the actual file URL
                        download_url=item['download'],
                        link=item['link'],
                        host=item['host']
                    )
                    downloads.append(download_item)
                except KeyError as e:
                    log.warning(f"Skipping download item due to missing key: {e}. Item: {item}")
                    # Decide whether to raise an error or just skip incomplete items
                    # Skipping seems more robust for now.

            log.info(f"Successfully fetched {len(downloads)} download items.")
            return downloads

        except requests.exceptions.JSONDecodeError as e:
            log.error(f"Failed to decode JSON response from {DOWNLOADS_ENDPOINT}: {e}")
            raise RealDebridError("Failed to decode API response.") from e
        except RealDebridError: # Re-raise specific errors
            raise
        except Exception as e: # Catch any other unexpected errors
            log.exception(f"An unexpected error occurred while fetching RD downloads: {e}")
            raise RealDebridError(f"An unexpected error occurred: {e}") from e

# --- Example Usage (for testing this module directly) ---
if __name__ == "__main__":
    load_dotenv()

    print("Attempting to fetch Real-Debrid downloads...")
    # Setup basic logging to console for testing
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    try:
        # Ensure the environment variable is set before running
        # export REAL_DEBRID_TOKEN="YOUR_ACTUAL_TOKEN_HERE"
        client = RealDebridClient()
        my_downloads = client.get_downloads()

        if my_downloads:
            print(f"\nFound {len(my_downloads)} downloads:")
            for i, dl in enumerate(my_downloads, 1):
                size_mb = dl.filesize / (1024 * 1024)
                print(f"{i:>3}: {dl.filename} ({size_mb:.2f} MB)")
        else:
            print("No downloads found in your Real-Debrid list.")

    except TokenError as e:
        print(f"\nError: {e}")
        print("Please ensure the REAL_DEBRID_TOKEN environment variable is set correctly.")
    except RealDebridError as e:
        print(f"\nAn error occurred interacting with Real-Debrid: {e}")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
