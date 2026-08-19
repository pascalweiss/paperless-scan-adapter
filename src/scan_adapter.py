#!/usr/bin/env python3
"""
Paperless Scan Adapter - Monitors Samba scan folder and uploads PDFs to Paperless-NGX
"""

import os
import sys
import json
import time
import logging
import threading
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# Paperless versions its REST API through the Accept header (DRF
# AcceptHeaderVersioning). A request without a version gets the server's
# DEFAULT_VERSION, which moved from 9 to 10 in Paperless 3.0, so an unpinned
# client silently changes behaviour the moment the server is upgraded.
# Raise this deliberately after checking the changes for the calls below.
PAPERLESS_API_VERSION = os.getenv('PAPERLESS_API_VERSION', '9')
PAPERLESS_ACCEPT_HEADER = f'application/json; version={PAPERLESS_API_VERSION}'

# Health endpoint. The heartbeat must outlive any single blocking call the worker
# makes; the upload request timeout of 60s is the longest one, hence the default of
# 120s. Deliberate waits keep beating, see sleep_with_heartbeat.
HEALTH_PORT = int(os.getenv('HEALTH_PORT', '8080'))
HEALTH_STALE_AFTER_SECONDS = int(os.getenv('HEALTH_STALE_AFTER_SECONDS', '120'))
HEARTBEAT_SLICE_SECONDS = int(os.getenv('HEARTBEAT_SLICE_SECONDS', '5'))

# Startup waits for things that may simply not be up yet (the SMB mount, Paperless).
STARTUP_RETRY_BASE_WAIT_SECONDS = int(os.getenv('STARTUP_RETRY_BASE_WAIT_SECONDS', '5'))
STARTUP_RETRY_MAX_WAIT_SECONDS = int(os.getenv('STARTUP_RETRY_MAX_WAIT_SECONDS', '60'))

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


class HealthState:
    """Liveness and readiness state, shared between the worker and the HTTP thread.

    The worker calls beat() whenever it reaches a line of code. A wedged worker (a
    blocking read on a stale SMB handle, a deadlock) stops beating while the HTTP
    thread keeps answering, which is exactly the condition a liveness probe has to
    catch. Slow work is not a wedge, so every deliberate wait beats as well.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._last_beat_at = time.time()
        self._startup_complete = False
        self._last_auth_ok_at: Optional[float] = None
        self._uploads = 0
        self._upload_failures = 0

    def beat(self) -> None:
        with self._lock:
            self._last_beat_at = time.time()

    def startup_completed(self) -> None:
        with self._lock:
            self._startup_complete = True

    def auth_succeeded(self) -> None:
        with self._lock:
            self._last_auth_ok_at = time.time()

    def upload_recorded(self, *, success: bool) -> None:
        with self._lock:
            if success:
                self._uploads += 1
            else:
                self._upload_failures += 1

    def snapshot(self) -> Dict:
        """Current state plus the derived verdicts, as one consistent reading."""
        with self._lock:
            now = time.time()
            beat_age = now - self._last_beat_at
            # While starting up we are deliberately retrying, which is alive but not
            # ready. Killing the pod here would only restart the same wait.
            alive = (not self._startup_complete) or beat_age <= HEALTH_STALE_AFTER_SECONDS
            ready = self._startup_complete and beat_age <= HEALTH_STALE_AFTER_SECONDS
            return {
                "alive": alive,
                "ready": ready,
                "startup_complete": self._startup_complete,
                "seconds_since_last_beat": round(beat_age, 1),
                "stale_after_seconds": HEALTH_STALE_AFTER_SECONDS,
                "uptime_seconds": round(now - self._started_at, 1),
                "last_auth_ok_age_seconds": (
                    round(now - self._last_auth_ok_at, 1) if self._last_auth_ok_at else None
                ),
                "uploads": self._uploads,
                "upload_failures": self._upload_failures,
                "scan_folder": str(SCAN_FOLDER_PATH),
            }


health = HealthState()


def sleep_with_heartbeat(seconds: float) -> None:
    """Sleep in slices so a deliberate wait never looks like a wedge."""
    deadline = time.monotonic() + seconds
    while True:
        health.beat()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(HEARTBEAT_SLICE_SECONDS, remaining))


METRIC_PREFIX = 'paperless_scan_adapter'

# name -> (type, help, key in the health snapshot)
METRICS = (
    ('alive', 'gauge', 'Whether the worker is not wedged (1) or its heartbeat went stale (0).', 'alive'),
    ('ready', 'gauge', 'Whether startup finished and the worker heartbeat is fresh.', 'ready'),
    ('seconds_since_last_beat', 'gauge', 'Age of the worker heartbeat in seconds.', 'seconds_since_last_beat'),
    ('uptime_seconds', 'gauge', 'Seconds since the process started.', 'uptime_seconds'),
    ('uploads_total', 'counter', 'Documents successfully handed to Paperless.', 'uploads'),
    ('upload_failures_total', 'counter', 'Documents archived after the upload failed.', 'upload_failures'),
)


def render_metrics(state: Dict) -> str:
    """Render the health snapshot in the Prometheus text exposition format."""
    lines = []
    for name, kind, description, key in METRICS:
        full = f"{METRIC_PREFIX}_{name}"
        value = state[key]
        if isinstance(value, bool):
            value = int(value)
        lines.append(f"# HELP {full} {description}")
        lines.append(f"# TYPE {full} {kind}")
        lines.append(f"{full} {value}")
    return "\n".join(lines) + "\n"


class HealthHandler(BaseHTTPRequestHandler):
    """Serves /healthz (liveness), /readyz (readiness) and /metrics."""

    protocol_version = 'HTTP/1.1'

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        state = health.snapshot()

        if path in ('/healthz', '/'):
            self._respond(200 if state["alive"] else 503, state)
        elif path == '/readyz':
            self._respond(200 if state["ready"] else 503, state)
        elif path == '/metrics':
            self._respond_text(200, render_metrics(state))
        else:
            self._respond(404, {"error": "not found", "paths": ["/healthz", "/readyz", "/metrics"]})

    def _respond(self, status: int, payload: Dict) -> None:
        body = json.dumps(payload, indent=2).encode() + b"\n"
        self._write(status, 'application/json', body)

    def _respond_text(self, status: int, text: str) -> None:
        self._write(status, 'text/plain; version=0.0.4; charset=utf-8', text.encode())

    def _write(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # The default implementation writes every request to stderr, which would
        # drown the application log once a probe runs every few seconds.
        logger.debug("health: %s", format % args)


def start_health_server() -> None:
    """Start the health endpoint in a daemon thread, before anything can block."""
    server = ThreadingHTTPServer(('0.0.0.0', HEALTH_PORT), HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name='health', daemon=True)
    thread.start()
    logger.info(f"Health endpoint listening on :{HEALTH_PORT} (/healthz, /readyz)")


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
            sleep_with_heartbeat(wait_time)

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
            headers={'Accept': PAPERLESS_ACCEPT_HEADER},
            timeout=10
        )

        if response.status_code == 200:
            token = response.json().get('token')
            logger.debug("Authentication successful")
            health.auth_succeeded()
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
            headers = {
                'Authorization': f'Token {token}',
                'Accept': PAPERLESS_ACCEPT_HEADER,
            }

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
            sleep_with_heartbeat(wait_time)

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
        health.upload_recorded(success=True)
        delete_file(filepath)
        return True
    else:
        # Upload failed after retries, move to archive
        health.upload_recorded(success=False)
        logger.warning(f"Archiving file after upload failure: {filepath.name}")
        move_to_archive(filepath)
        return True


def wait_until(condition, description: str) -> None:
    """Block until condition() is true, with capped exponential backoff.

    Used for startup dependencies that are expected to arrive on their own. The wait
    is unbounded on purpose: there is no useful number of attempts after which giving
    up beats keeping the pod alive and ready to work the moment the dependency shows.
    """
    attempt = 0
    while not condition():
        wait_time = min(
            STARTUP_RETRY_BASE_WAIT_SECONDS * (2 ** attempt),
            STARTUP_RETRY_MAX_WAIT_SECONDS,
        )
        logger.warning(
            f"Waiting for {description}, not available yet "
            f"(attempt {attempt + 1}, retrying in {wait_time}s)"
        )
        sleep_with_heartbeat(wait_time)
        attempt += 1

    if attempt:
        logger.info(f"{description} became available after {attempt + 1} attempts")
    else:
        logger.info(f"{description} available")


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

    # The health endpoint comes up before any check that can block or wait, so a
    # probe gets an answer while we are still waiting for our dependencies.
    start_health_server()

    # A missing password is a configuration error. No amount of retrying fixes it,
    # so this is the one startup condition that still exits.
    if not PAPERLESS_ADMIN_PASSWORD:
        logger.error("PAPERLESS_ADMIN_PASSWORD not set, this is a configuration error")
        sys.exit(1)

    # The mount and Paperless may simply not be up yet. Waiting for them is the job,
    # not a failure: exiting here would only hand the same wait back to Kubernetes,
    # inflate the restart counter and delay the start by the CrashLoopBackOff.
    wait_until(
        lambda: SCAN_FOLDER_PATH.exists(),
        description=f"scan folder {SCAN_FOLDER_PATH}",
    )
    wait_until(
        lambda: authenticate_paperless() is not None,
        description=f"Paperless at {PAPERLESS_API_URL}",
    )
    logger.info("All dependencies reachable, entering processing loop")
    health.startup_completed()

    # Main processing loop
    while True:
        try:
            health.beat()
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
            sleep_with_heartbeat(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal, exiting...")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            sleep_with_heartbeat(SCAN_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
