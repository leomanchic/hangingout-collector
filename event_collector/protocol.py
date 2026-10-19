import hashlib
import hmac

ENDPOINT = "/api/internal/collector/yandex"
MAX_BYTES = 262_144

def signature(secret, stamp, body):
    # Bind to this operation, timestamp and exact bytes. The key never travels.
    message = b"hangingout-collector-v1\n" + stamp.encode("ascii") + b"\n" + body
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()
