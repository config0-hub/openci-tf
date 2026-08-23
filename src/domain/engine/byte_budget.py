"""Payload bound leaves headroom for both dispatch paths."""
from src.core.errors import PayloadTooLargeError
MAX_SERIALIZED_PAYLOAD_BYTES = 131072
def check_payload_size(serialized: bytes) -> None:
    if len(serialized) > MAX_SERIALIZED_PAYLOAD_BYTES: raise PayloadTooLargeError("serialized payload exceeds 128 KiB")
