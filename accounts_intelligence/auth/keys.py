"""Ed25519 identity of a site. The private key never leaves the site; only the public key is shared."""

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def generate_keypair() -> tuple[str, str]:
	"""Return (private_key, public_key), each the base64 of the raw 32-byte key."""
	private = Ed25519PrivateKey.generate()
	private_raw = private.private_bytes(
		serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
	)
	public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
	return _encode(private_raw), _encode(public_raw)


def is_valid_public_key(public_key) -> bool:
	try:
		_public(public_key)
		return True
	except ValueError:
		return False


def key_id(public_key: str) -> str:
	"""Full SHA-256 of the raw public key. Requests carry it to name the key they were signed with."""
	return hashlib.sha256(_decode(public_key, 32)).hexdigest()


def fingerprint(public_key: str) -> str:
	"""First 128 bits of the key id, grouped so two people can compare it by eye."""
	digest = key_id(public_key)[:32].upper()
	return " ".join(digest[i : i + 4] for i in range(0, 32, 4))


def sign(private_key: str, message: bytes) -> str:
	return _encode(Ed25519PrivateKey.from_private_bytes(_decode(private_key, 32)).sign(message))


def verify(public_key, message: bytes, signature) -> bool:
	try:
		_public(public_key).verify(_decode(signature, 64), message)
		return True
	except (InvalidSignature, ValueError):
		return False


def _public(public_key) -> Ed25519PublicKey:
	return Ed25519PublicKey.from_public_bytes(_decode(public_key, 32))


def _encode(raw: bytes) -> str:
	return base64.b64encode(raw).decode()


def _decode(value, length: int) -> bytes:
	if not isinstance(value, str):
		raise ValueError("expected base64 text")
	try:
		raw = base64.b64decode(value.encode(), validate=True)
	except (binascii.Error, ValueError):
		raise ValueError("not valid base64")
	if len(raw) != length:
		raise ValueError("wrong length")
	return raw
