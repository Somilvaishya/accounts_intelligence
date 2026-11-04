"""Request and response signing, protocol AI1.

A request signature covers the method, the path, the server the request is meant for, the sending site,
a timestamp, a single-use nonce and a hash of the body. A captured request therefore cannot be changed,
sent to another server, or replayed. A response signature covers the request's nonce, so an old reply
cannot be passed off as the answer to a new request.
"""

import hashlib
import json
import re
import secrets
import time

from accounts_intelligence.auth import keys

PROTOCOL = "AI1"
HEADER_SITE = "X-AI-Site"
HEADER_KEY = "X-AI-Key"
HEADER_TIMESTAMP = "X-AI-Timestamp"
HEADER_NONCE = "X-AI-Nonce"
HEADER_SIGNATURE = "X-AI-Signature"
MAX_CLOCK_SKEW = 300
NONCE_TTL = 2 * MAX_CLOCK_SKEW
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


class BadSignature(ValueError):
	pass


def body_hash(body: bytes) -> str:
	return hashlib.sha256(body).hexdigest()


def within_window(timestamp: int) -> bool:
	return abs(time.time() - timestamp) <= MAX_CLOCK_SKEW


def request_message(method, path, audience, site, timestamp, nonce, body: bytes) -> bytes:
	return "\n".join(
		(PROTOCOL, method.upper(), path, audience, site, str(timestamp), nonce, body_hash(body))
	).encode()


def new_nonce() -> str:
	return secrets.token_hex(16)


def sign_request(private_key, public_key, method, path, audience, site, body: bytes, nonce=None) -> dict:
	timestamp = int(time.time())
	nonce = nonce or new_nonce()
	signature = keys.sign(private_key, request_message(method, path, audience, site, timestamp, nonce, body))
	return {
		HEADER_SITE: site,
		HEADER_KEY: keys.key_id(public_key),
		HEADER_TIMESTAMP: str(timestamp),
		HEADER_NONCE: nonce,
		HEADER_SIGNATURE: signature,
	}


def rotation_statement(site, old_public_key, new_public_key, nonce) -> bytes:
	"""What the new key signs during a key rotation: it binds the new key to the old one, the site and
	the one request that carries it, so the proof cannot be reused for another site or another request."""
	return "\n".join(
		(f"{PROTOCOL}-ROTATE", site, keys.key_id(old_public_key), keys.key_id(new_public_key), nonce)
	).encode()


def response_message(request_nonce, timestamp, payload: str) -> bytes:
	return "\n".join(
		(f"{PROTOCOL}-RESPONSE", request_nonce, str(timestamp), body_hash(payload.encode()))
	).encode()


def sign_response(private_key, request_nonce, data: dict) -> dict:
	payload = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
	timestamp = int(time.time())
	return {
		"protocol": PROTOCOL,
		"payload": payload,
		"timestamp": timestamp,
		"signature": keys.sign(private_key, response_message(request_nonce, timestamp, payload)),
	}


def open_response(public_key, request_nonce, envelope) -> dict:
	"""Verify a signed reply against the pinned server key and return its data."""
	if not isinstance(envelope, dict) or envelope.get("protocol") != PROTOCOL:
		raise BadSignature("the reply is not signed")
	payload, timestamp, signature = (
		envelope.get("payload"),
		envelope.get("timestamp"),
		envelope.get("signature"),
	)
	if not isinstance(payload, str) or not isinstance(timestamp, int) or not isinstance(signature, str):
		raise BadSignature("the signed reply is incomplete")
	if not within_window(timestamp):
		raise BadSignature("the reply's timestamp is outside the allowed window")
	if not keys.verify(public_key, response_message(request_nonce, timestamp, payload), signature):
		raise BadSignature("the reply's signature does not match the server's key")
	data = json.loads(payload)
	if not isinstance(data, dict):
		raise BadSignature("the signed reply is not an object")
	return data
