"""Header branding (issue #45): a custom app name and logo, kept in ADMIN_DATA_DIR/customization/.

``config.json`` holds the name and the logo's detected type and sha256; the logo itself is
``logo.<ext>``. Both are regular files (not bind-mount sources), so writes use tmp+rename.
The type is decided from the file's leading bytes, never from the upload's name or
Content-Type, and only raster formats are accepted: an SVG can carry script.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_NAME = "AzerothCore Admin"
MAX_NAME_LENGTH = 32
MAX_LOGO_BYTES = 512 * 1024

# logo_type -> (file extension, media type)
LOGO_TYPES: dict[str, tuple[str, str]] = {
    "png": ("png", "image/png"),
    "jpeg": ("jpg", "image/jpeg"),
    "gif": ("gif", "image/gif"),
    "webp": ("webp", "image/webp"),
}


class CustomizationError(ValueError):
    """A name or logo the admin refuses to store; the message is shown to the user."""


@dataclass(frozen=True)
class CustomizationConfig:
    app_name: str = ""
    logo_type: str | None = None
    logo_sha256: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CustomizationConfig":
        if not isinstance(raw, dict):
            raise ValueError("customization settings must be an object")
        name = raw.get("app_name", "")
        logo_type = raw.get("logo_type")
        logo_sha256 = raw.get("logo_sha256")
        if not isinstance(name, str):
            raise ValueError("app_name must be a string")
        if logo_type is not None and logo_type not in LOGO_TYPES:
            raise ValueError("logo_type is not a supported image type")
        if logo_sha256 is not None and not (
            isinstance(logo_sha256, str)
            and len(logo_sha256) == 64
            and all(c in "0123456789abcdef" for c in logo_sha256)
        ):
            raise ValueError("logo_sha256 must be 64 lowercase hex characters")
        if (logo_type is None) != (logo_sha256 is None):
            raise ValueError("logo_type and logo_sha256 must be set together")
        return cls(app_name=name, logo_type=logo_type, logo_sha256=logo_sha256)


@dataclass(frozen=True)
class Branding:
    name: str
    custom_name: str
    logo_url: str | None


def normalize_name(name: str) -> str:
    """The name as stored: trimmed, at most MAX_NAME_LENGTH characters, no control characters."""
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise CustomizationError(f"The name can be at most {MAX_NAME_LENGTH} characters.")
    # Cc/Zl/Zp: line breaks, tabs, NUL and the like. Format characters (Cf) stay: emoji need ZWJ.
    if any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in name):
        raise CustomizationError("The name cannot contain line breaks, tabs or other control characters.")
    return name


def detect_logo_type(data: bytes) -> str:
    if len(data) > MAX_LOGO_BYTES:
        raise CustomizationError("The logo can be at most 512 KB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise CustomizationError("The logo must be a PNG, JPG, WebP or GIF image.")


class CustomizationStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "customization"
        self.config_path = self.dir / "config.json"

    def load(self) -> CustomizationConfig:
        try:
            return CustomizationConfig.from_dict(json.loads(self.config_path.read_text()))
        except FileNotFoundError:
            return CustomizationConfig()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            log.warning("customization settings %s unreadable; using defaults", self.config_path)
            return CustomizationConfig()

    def _logo_path(self, logo_type: str) -> Path:
        return self.dir / f"logo.{LOGO_TYPES[logo_type][0]}"

    def logo(self) -> tuple[bytes, str] | None:
        """The stored logo's bytes and media type, or None when there is none (or it is unreadable)."""
        cfg = self.load()
        if cfg.logo_type is None:
            return None
        try:
            data = self._logo_path(cfg.logo_type).read_bytes()
        except OSError:
            return None
        return data, LOGO_TYPES[cfg.logo_type][1]

    def branding(self) -> Branding:
        cfg = self.load()
        logo_url = None
        if cfg.logo_type is not None and self._logo_path(cfg.logo_type).is_file():
            logo_url = f"/customization/logo?v={cfg.logo_sha256[:12]}"
        return Branding(name=cfg.app_name or DEFAULT_NAME, custom_name=cfg.app_name, logo_url=logo_url)

    def save(self, *, name: str, logo: bytes | None, remove_logo: bool) -> CustomizationConfig:
        """Store a new name and, optionally, replace or remove the logo.

        Everything is validated before anything is written. The logo file is written before
        the settings that point at it; stale ``logo.*`` files are removed afterwards.
        """
        name = normalize_name(name)
        logo_type = detect_logo_type(logo) if logo is not None else None
        current = self.load()
        self.dir.mkdir(parents=True, exist_ok=True)
        if logo is not None:
            path = self._logo_path(logo_type)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(logo)
            os.replace(tmp, path)
            cfg = CustomizationConfig(name, logo_type, hashlib.sha256(logo).hexdigest())
        elif remove_logo:
            cfg = CustomizationConfig(name)
        else:
            cfg = CustomizationConfig(name, current.logo_type, current.logo_sha256)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.config_path)
        keep = self._logo_path(cfg.logo_type).name if cfg.logo_type else None
        for ext, _media in LOGO_TYPES.values():
            stale = self.dir / f"logo.{ext}"
            if stale.name != keep:
                stale.unlink(missing_ok=True)
        return cfg


def store_from_env() -> CustomizationStore:
    return CustomizationStore(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")))
