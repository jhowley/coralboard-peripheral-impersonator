"""Bounded I2C register-map profile: load, validate, canonicalise.

A profile is a *bounding box*, not a device model. `fidelity` must be
"synthetic-bounded" and `claimed_device` must be null, so no profile can
pretend to be a faithful TMP102 (or anything else).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import zlib
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
FIDELITY = "synthetic-bounded"
BENCH_ADDRESS = 0x48
REGISTER_SPACE = 256
LEASE_MAX_MS = 10000

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_HEX_RE = re.compile(r"^[0-9a-f]*$")

REQUIRED_FIELDS = (
    "schema_version",
    "profile_id",
    "fidelity",
    "claimed_device",
    "address",
    "register_space",
    "defaults",
    "writable",
    "read_policy",
    "lease_max_ms",
)


class ProfileError(ValueError):
    """Raised when a profile document is missing, malformed or out of bounds."""

    def __init__(self, issues):
        self.issues = list(issues)
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in self.issues))

    def codes(self):
        return [i.code for i in self.issues]


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


@dataclass(frozen=True)
class Profile:
    profile_id: str
    address: int
    register_space: int
    image: bytes
    writable: frozenset
    auto_increment: bool
    pointer_wrap: int
    pointer_initial: int
    lease_max_ms: int
    fidelity: str
    digest: str
    raw: dict

    def describe(self) -> str:
        return (
            f"{self.profile_id} addr=0x{self.address:02x} space={self.register_space} "
            f"auto_increment={self.auto_increment} wrap={self.pointer_wrap} "
            f"initial={self.pointer_initial} "
            f"writable={sorted(self.writable)} lease_max_ms={self.lease_max_ms} "
            f"digest={self.digest[:16]}"
        )


def wmask_bytes(profile) -> bytes:
    """Wire/device write mask: one bit per writable register."""
    mask = bytearray(profile.register_space // 8)
    for reg in profile.writable:
        mask[reg >> 3] |= 1 << (reg & 7)
    return bytes(mask)


def config_crc(profile) -> int:
    """Same value as firmware cb_core_config_crc().

    tag || NUL || image || wmask || flags || wrap_le16 || initial.
    """
    flags = 0x01 if profile.auto_increment else 0x00
    blob = (
        profile.profile_id.encode("ascii") + b"\x00"
        + bytes(profile.image)
        + wmask_bytes(profile)
        + bytes([flags])
        + int(profile.pointer_wrap).to_bytes(2, "little")
        + bytes([profile.pointer_initial])
    )
    return zlib.crc32(blob) & 0xFFFFFFFF


def canonical_bytes(doc: dict) -> bytes:
    """Order-independent canonical JSON, so identical profiles hash identically."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_profile(doc: Any, source: str = "<memory>") -> Profile:
    issues = []

    def add(code, message):
        issues.append(Issue(code, message))

    if not isinstance(doc, dict):
        raise ProfileError([Issue("not_object", f"{source}: profile must be a JSON object")])

    for field in REQUIRED_FIELDS:
        if field not in doc:
            add("missing_field", f"missing required field '{field}'")

    if "schema_version" in doc and doc["schema_version"] != SCHEMA_VERSION:
        add("schema_version", f"schema_version must be {SCHEMA_VERSION}")
    if "fidelity" in doc and doc["fidelity"] != FIDELITY:
        add("fidelity", f"fidelity must be '{FIDELITY}' (bench claims no device fidelity)")
    if "claimed_device" in doc and doc["claimed_device"] is not None:
        add("claimed_device", "claimed_device must be null: profiles are synthetic bounds")

    profile_id = doc.get("profile_id", "")
    if "profile_id" in doc and (not isinstance(profile_id, str) or not _PROFILE_ID_RE.match(profile_id)):
        add("profile_id", "profile_id must match ^[a-z0-9][a-z0-9._-]{0,31}$")

    address = doc.get("address", BENCH_ADDRESS)
    if "address" in doc and (not _is_int(address) or address != BENCH_ADDRESS):
        add("address", f"address must be 0x{BENCH_ADDRESS:02x} (bench fixture address)")

    space = doc.get("register_space", REGISTER_SPACE)
    if "register_space" in doc and (not _is_int(space) or space != REGISTER_SPACE):
        add("register_space", f"register_space must be {REGISTER_SPACE} (fixed-size image)")

    image = b""
    defaults = doc.get("defaults")
    if "defaults" in doc:
        if not isinstance(defaults, dict) or "image_hex" not in defaults:
            add("defaults", "defaults must be an object with 'image_hex'")
        else:
            hex_image = defaults["image_hex"]
            if not isinstance(hex_image, str) or not _HEX_RE.match(hex_image):
                add("image_hex", "image_hex must be lowercase hex")
            elif len(hex_image) != REGISTER_SPACE * 2:
                add("image_length", f"image_hex must be {REGISTER_SPACE * 2} chars")
            else:
                image = bytes.fromhex(hex_image)

    writable = []
    if "writable" in doc:
        raw_writable = doc["writable"]
        if not isinstance(raw_writable, list):
            add("writable_type", "writable must be a list of register numbers")
        else:
            for reg in raw_writable:
                if not _is_int(reg):
                    add("writable_type", f"writable entry {reg!r} is not an integer")
                elif not 0 <= reg < REGISTER_SPACE:
                    add("writable_range", f"writable register {reg} outside 0..{REGISTER_SPACE - 1}")
                elif reg not in writable:
                    writable.append(reg)

    auto_increment = False
    pointer_wrap = 1
    policy = doc.get("read_policy")
    if "read_policy" in doc:
        if not isinstance(policy, dict):
            add("read_policy", "read_policy must be an object")
        else:
            auto_increment = policy.get("auto_increment", False)
            if not isinstance(auto_increment, bool):
                add("read_policy", "read_policy.auto_increment must be a boolean")
                auto_increment = False
            pointer_wrap = policy.get("pointer_wrap", 1)
            if not _is_int(pointer_wrap) or pointer_wrap < 1:
                add("pointer_wrap", "read_policy.pointer_wrap must be an integer >= 1")
                pointer_wrap = 1
            elif pointer_wrap > REGISTER_SPACE:
                add("pointer_wrap", f"read_policy.pointer_wrap must be <= {REGISTER_SPACE}")

    lease_max_ms = doc.get("lease_max_ms", 0)
    if "lease_max_ms" in doc:
        if not _is_int(lease_max_ms) or not 1 <= lease_max_ms <= LEASE_MAX_MS:
            add("lease_bound", f"lease_max_ms must be 1..{LEASE_MAX_MS} (bounded lease)")

    pointer_register = doc.get("pointer_register", {"size": 1, "initial": 0})
    pointer_initial = 0
    if not isinstance(pointer_register, dict):
        add("pointer_register", "pointer_register must be an object")
    else:
        if pointer_register.get("size", 1) != 1:
            add("pointer_register", "pointer_register.size must be 1")
        initial = pointer_register.get("initial", 0)
        if not _is_int(initial) or not 0 <= initial < REGISTER_SPACE:
            add("pointer_register", "pointer_register.initial out of range")
        else:
            pointer_initial = initial
            if initial >= pointer_wrap:
                add("pointer_register",
                    f"pointer_register.initial {initial} must be < pointer_wrap {pointer_wrap}")

    if issues:
        raise ProfileError(issues)

    digest = hashlib.sha256(canonical_bytes(doc)).hexdigest()
    return Profile(
        profile_id=profile_id,
        address=address,
        register_space=space,
        image=image,
        writable=frozenset(writable),
        auto_increment=auto_increment,
        pointer_wrap=pointer_wrap,
        pointer_initial=pointer_initial,
        lease_max_ms=lease_max_ms,
        fidelity=FIDELITY,
        digest=digest,
        raw=copy.deepcopy(doc),
    )


def load_profile(path) -> Profile:
    path = os.fspath(path)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise ProfileError([Issue("io", f"{path}: {exc.strerror or exc}")]) from exc
    except json.JSONDecodeError as exc:
        raise ProfileError([Issue("json", f"{path}: {exc.msg} at line {exc.lineno}")]) from exc
    try:
        return validate_profile(doc, source=path)
    except ProfileError as exc:
        raise ProfileError([Issue(i.code, f"{path}: {i.message}") for i in exc.issues]) from exc
