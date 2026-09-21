"""YAML configuration loader and validation.

Profile resolution order (Multi-Profile-Support):
  1. ``profile`` parameter passed to :func:`load_config`
  2. ``JOBFINDER_PROFILE`` environment variable
  3. Fallback ``example``

For a profile ``<name>``, the loader looks for:
  - ``config/profile_<name>.yaml`` (preferred)
  - ``config/profile.yaml`` (legacy fallback — only used when no profile name is
    given AND ``profile_<name>.yaml`` doesn't exist).

This keeps ``profile.yaml`` working for the single-profile installations that
existed before the refactor.
"""

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_PROFILE = "example"


def resolve_profile(profile: str | None = None) -> str:
    """Resolve a profile name from explicit arg → env var → default."""
    return profile or os.getenv("JOBFINDER_PROFILE") or DEFAULT_PROFILE


def config_path_for_profile(profile: str) -> Path:
    """Return the YAML path for a profile, with legacy fallback to profile.yaml."""
    specific = CONFIG_DIR / f"profile_{profile}.yaml"
    if specific.exists():
        return specific
    legacy = CONFIG_DIR / "profile.yaml"
    if legacy.exists():
        return legacy
    return specific  # let load_config raise with the expected name


def load_config(
    path: Path | str | None = None,
    profile: str | None = None,
) -> dict:
    """Load and validate the YAML configuration file.

    Args:
        path: Explicit path to a YAML file. If given, ``profile`` is ignored.
        profile: Profile name (e.g. ``"example"``). Looked up as
                 ``config/profile_<name>.yaml``.
    """
    if path is not None:
        config_path = Path(path)
    else:
        active_profile = resolve_profile(profile)
        config_path = config_path_for_profile(active_profile)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config.setdefault("_meta", {})["config_path"] = str(config_path)
    config["_meta"]["profile"] = resolve_profile(profile)

    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    """Basic validation of required config sections."""
    required_sections = ["profile", "search", "preferences", "scrapers", "output"]
    missing = [s for s in required_sections if s not in config]
    if missing:
        raise ValueError(f"Missing config sections: {', '.join(missing)}")

    search = config["search"]
    if not search.get("keywords"):
        raise ValueError("Config must have at least one search keyword")
    if not search.get("location"):
        raise ValueError("Config must specify a location")

    output = config["output"]
    if not output.get("database_path"):
        raise ValueError("Config must specify output.database_path")
