"""CI entry point. The ingestion key is never committed, printed or put in arguments."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main():
    source = os.environ.get("COLLECTOR_SOURCE", "")
    secret = os.environ.pop("COLLECTOR_SECRET", "")
    if source not in {"kudago", "yandex"} or not re.fullmatch(r"[a-f0-9]{64}", secret):
        print("Collector configuration missing or invalid. Set COLLECTOR_SECRET in Actions secrets.")
        return 1
    # TemporaryDirectory is outside the checkout and is removed even on failures.
    with tempfile.TemporaryDirectory(prefix="event-collector-") as directory:
        key_file = Path(directory) / "ingest.key"
        with key_file.open("x", encoding="ascii") as stream:
            os.chmod(key_file, 0o600)
            stream.write(secret)
        secret = None
        try:
            result = subprocess.run([sys.executable, "-m", f"event_collector.{source}_collect",
                                     "--channel", "chromium", "--publish", "--key-file", str(key_file)],
                                    timeout=960, check=False)
            return result.returncode
        except subprocess.TimeoutExpired:
            print("Collector timed out. Check the last successful receipt; existing catalogue is preserved.")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
