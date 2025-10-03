#!/usr/bin/env python3
"""
Paperless Scan Adapter - Monitors Samba scan folder and uploads PDFs to Paperless-NGX
"""

import os
import sys
import time
import logging
import requests
from pathlib import Path
from typing import Optional, List, Dict

# Configuration from environment variables
VALIDATION_RETRY_COUNT = int(os.getenv('VALIDATION_RETRY_COUNT', '5'))
VALIDATION_RETRY_BASE_WAIT_SECONDS = int(os.getenv('VALIDATION_RETRY_BASE_WAIT_SECONDS', '10'))
UPLOAD_RETRY_COUNT = int(os.getenv('UPLOAD_RETRY_COUNT', '3'))
UPLOAD_RETRY_BASE_WAIT_SECONDS = int(os.getenv('UPLOAD_RETRY_BASE_WAIT_SECONDS', '5'))
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '5'))
PAPERLESS_API_URL = os.getenv('PAPERLESS_API_URL', 'http://paperless-ngx.paperless-ngx.svc.cluster.local:8000')
SCAN_FOLDER_PATH = Path(os.getenv('SCAN_FOLDER_PATH', '/mnt/scan/scan'))
ARCHIVE_FOLDER_PATH = Path(os.getenv('ARCHIVE_FOLDER_PATH', '/mnt/scan/scan/archive'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
PAPERLESS_ADMIN_USER = os.getenv('PAPERLESS_ADMIN_USER', 'admin')
PAPERLESS_ADMIN_PASSWORD = os.getenv('PAPERLESS_ADMIN_PASSWORD', '')

# Global retry state to track validation attempts for invalid files
# Structure: {file_path: {"retry_count": int, "next_retry_time": float}}
retry_state: Dict[str, Dict] = {}

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def get_pdf_files(folder: Path) -> List[Path]:
    """Get all PDF files in the folder, sorted by name."""
    try:
        pdf_files = sorted(folder.glob('*.pdf'))
        return pdf_files
    except Exception as e:
        logger.error(f"Error scanning folder {folder}: {e}")
        return []


def is_pdf_valid(filepath: Path) -> bool:
    """Check whether the PDF file ends with an EOF marker.

    The PDF spec requires the final non-whitespace characters to be ``%%EOF``. Many
    scanners use Windows style ``\r\n`` endings or add trailing whitespace, so we
    trim trailing whitespace before checking for the marker.
    """

    try:
        with open(filepath, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()

            if file_size == 0:
                logger.debug(f"PDF invalid (empty file): {filepath.name}")
                return False

            chunk_size = min(1024, file_size)
            f.seek(-chunk_size, os.SEEK_END)
            tail = f.read()

        trimmed_tail = tail.rstrip(b"\x00\t\n\r \f")
        is_valid = trimmed_tail.endswith(b'%%EOF')

        if is_valid:
            logger.debug(f"PDF valid: {filepath.name}")
        else:
            logger.debug(f"PDF invalid (missing EOF): {filepath.name}")

        return is_valid
    except Exception as e:
        logger.error(f"Error validating PDF {filepath.name}: {e}")
        return False


def retry_validation_with_backoff(filepath: Path) -> bool:
    """Retry validation with exponential backoff."""
    logger.info(f"Starting validation retry for: {filepath.name}")

    for attempt in range(VALIDATION_RETRY_COUNT):
        if is_pdf_valid(filepath):
            logger.info(f"PDF became valid after {attempt + 1} attempts: {filepath.name}")
            return True

        if attempt < VALIDATION_RETRY_COUNT - 1:
            wait_time = VALIDATION_RETRY_BASE_WAIT_SECONDS * (2 ** attempt)
            logger.info(f"Validation retry {attempt + 1}/{VALIDATION_RETRY_COUNT} failed, waiting {wait_time}s")
            time.sleep(wait_time)

    logger.warning(f"PDF validation failed after {VALIDATION_RETRY_COUNT} retries: {filepath.name}")
    return False


def authenticate_paperless() -> Optional[str]:
    """Authenticate with Paperless API and return token."""
    try:
        url = f"{PAPERLESS_API_URL}/api/token/"
        response = requests.post(
            url,
            json={
                "username": PAPERLESS_ADMIN_USER,
                "password": PAPERLESS_ADMIN_PASSWORD
            },
            timeout=10
        )

        if response.status_code == 200:
            token = response.json().get('token')
            logger.debug("Authentication successful")
            return token
        else:
            logger.error(f"Authentication failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Authentication error: {e}")
        return None


def upload_file_to_paperless(filepath: Path, token: str) -> bool:
    """Upload a single file to Paperless."""
    try:
        url = f"{PAPERLESS_API_URL}/api/documents/post_document/"

        with open(filepath, 'rb') as f:
            files = {'document': (filepath.name, f, 'application/pdf')}
            headers = {'Authorization': f'Token {token}'}

            response = requests.post(
                url,
                files=files,
                headers=headers,
                timeout=60
            )

        if response.status_code == 200:
            task_id = response.json() if isinstance(response.json(), str) else response.text
            logger.info(f"Upload successful: {filepath.name} (task_id: {task_id})")
            return True
        else:
            logger.error(f"Upload failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Upload error for {filepath.name}: {e}")
        return False


def upload_to_paperless_with_retry(filepath: Path) -> bool:
    """Upload file to Paperless with retry logic."""
    logger.info(f"Starting upload: {filepath.name}")

    # Authenticate first
    token = authenticate_paperless()
    if not token:
        logger.error("Failed to authenticate with Paperless")
        return False

    # Try upload with exponential backoff
    for attempt in range(UPLOAD_RETRY_COUNT):
        if upload_file_to_paperless(filepath, token):
            return True

        if attempt < UPLOAD_RETRY_COUNT - 1:
            wait_time = UPLOAD_RETRY_BASE_WAIT_SECONDS * (2 ** attempt)
            logger.info(f"Upload retry {attempt + 1}/{UPLOAD_RETRY_COUNT} failed, waiting {wait_time}s")
            time.sleep(wait_time)

            # Re-authenticate for retry
            token = authenticate_paperless()
            if not token:
                logger.error("Failed to re-authenticate for retry")
                return False

    logger.error(f"Upload failed after {UPLOAD_RETRY_COUNT} retries: {filepath.name}")
    return False


def delete_file(filepath: Path) -> bool:
    """Delete a file."""
    try:
        filepath.unlink()
        logger.info(f"Deleted file: {filepath.name}")
        return True
    except Exception as e:
        logger.error(f"Error deleting file {filepath.name}: {e}")
        return False


def move_to_archive(filepath: Path) -> bool:
    """Move file to archive folder."""
    try:
        # Ensure archive folder exists
        ARCHIVE_FOLDER_PATH.mkdir(parents=True, exist_ok=True)

        destination = ARCHIVE_FOLDER_PATH / filepath.name

        # If file already exists in archive, add timestamp
        if destination.exists():
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            stem = filepath.stem
            suffix = filepath.suffix
            destination = ARCHIVE_FOLDER_PATH / f"{stem}_{timestamp}{suffix}"

        filepath.rename(destination)
        logger.info(f"Moved to archive: {filepath.name} -> {destination.name}")
        return True
    except Exception as e:
        logger.error(f"Error moving file to archive {filepath.name}: {e}")
        return False


def process_pdf_file(filepath: Path) -> bool:
    """Process a single PDF file. Returns True if file was handled (deleted or archived)."""
    logger.info(f"Processing: {filepath.name}")

    file_key = str(filepath)
    current_time = time.time()

    # Check if PDF is valid
    if not is_pdf_valid(filepath):
        # Initialize retry state for this file if not exists
        if file_key not in retry_state:
            retry_state[file_key] = {
                "retry_count": 0,
                "next_retry_time": current_time  # Can retry immediately on first attempt
            }

        state = retry_state[file_key]

        # Check if we should attempt retry now
        if current_time >= state["next_retry_time"]:
            # Time to retry validation
            retry_count = state["retry_count"]

            if retry_count >= VALIDATION_RETRY_COUNT:
                # Exceeded max retries, delete file
                logger.warning(f"File exceeded {VALIDATION_RETRY_COUNT} validation retries, deleting: {filepath.name}")
                delete_file(filepath)
                del retry_state[file_key]
                return True

            # Attempt validation
            logger.info(f"Attempting validation retry {retry_count + 1}/{VALIDATION_RETRY_COUNT} for: {filepath.name}")

            if is_pdf_valid(filepath):
                # File became valid! Clear retry state and continue to upload
                logger.info(f"File became valid after {retry_count + 1} attempts: {filepath.name}")
                del retry_state[file_key]
                # Continue to upload below
            else:
                # Still invalid, schedule next retry with exponential backoff
                wait_time = VALIDATION_RETRY_BASE_WAIT_SECONDS * (2 ** retry_count)
                next_retry = current_time + wait_time
                state["retry_count"] = retry_count + 1
                state["next_retry_time"] = next_retry

                logger.info(f"File still invalid, will retry in {wait_time}s (attempt {retry_count + 1}/{VALIDATION_RETRY_COUNT}): {filepath.name}")
                return False  # Skip for now, will retry later
        else:
            # Not time to retry yet, skip this file
            wait_remaining = int(state["next_retry_time"] - current_time)
            logger.debug(f"File in retry waiting period ({wait_remaining}s remaining): {filepath.name}")
            return False  # Non-blocking: continue to next file

    # PDF is valid (either was valid initially or became valid after retry)
    # Clear retry state if exists
    if file_key in retry_state:
        del retry_state[file_key]

    # Attempt upload
    if upload_to_paperless_with_retry(filepath):
        # Upload successful, delete file
        delete_file(filepath)
        return True
    else:
        # Upload failed after retries, move to archive
        logger.warning(f"Archiving file after upload failure: {filepath.name}")
        move_to_archive(filepath)
        return True


def main():
    """Main loop."""
    logger.info("=" * 60)
    logger.info("Paperless Scan Adapter Starting")
    logger.info("=" * 60)
    logger.info(f"Scan folder: {SCAN_FOLDER_PATH}")
    logger.info(f"Archive folder: {ARCHIVE_FOLDER_PATH}")
    logger.info(f"Paperless API: {PAPERLESS_API_URL}")
    logger.info(f"Scan interval: {SCAN_INTERVAL_SECONDS}s")
    logger.info(f"Validation retries: {VALIDATION_RETRY_COUNT} (base wait: {VALIDATION_RETRY_BASE_WAIT_SECONDS}s)")
    logger.info(f"Upload retries: {UPLOAD_RETRY_COUNT} (base wait: {UPLOAD_RETRY_BASE_WAIT_SECONDS}s)")
    logger.info("=" * 60)

    # Verify scan folder exists
    if not SCAN_FOLDER_PATH.exists():
        logger.error(f"Scan folder does not exist: {SCAN_FOLDER_PATH}")
        sys.exit(1)

    # Verify credentials
    if not PAPERLESS_ADMIN_PASSWORD:
        logger.error("PAPERLESS_ADMIN_PASSWORD not set")
        sys.exit(1)

    # Test authentication
    logger.info("Testing Paperless authentication...")
    token = authenticate_paperless()
    if not token:
        logger.error("Initial authentication test failed")
        sys.exit(1)
    logger.info("Authentication test successful")

    # Main processing loop
    while True:
        try:
            pdf_files = get_pdf_files(SCAN_FOLDER_PATH)

            # Cleanup retry state for files that no longer exist
            current_file_keys = {str(f) for f in pdf_files}
            keys_to_remove = [k for k in retry_state.keys() if k not in current_file_keys]
            for key in keys_to_remove:
                del retry_state[key]
                logger.debug(f"Removed retry state for deleted file: {key}")

            if pdf_files:
                logger.info(f"Found {len(pdf_files)} PDF file(s) to process")

                # Process files sequentially
                for pdf_file in pdf_files:
                    # Check if file still exists (might have been deleted/moved)
                    if not pdf_file.exists():
                        continue

                    process_pdf_file(pdf_file)
            else:
                logger.debug("No PDF files found")

            # Wait before next scan
            logger.debug(f"Waiting {SCAN_INTERVAL_SECONDS}s until next scan...")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal, exiting...")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
