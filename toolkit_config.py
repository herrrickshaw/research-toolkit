"""Optional per-machine configuration for person- or machine-specific values.

Nothing personal is stored in this repository. Values are read, in order, from

  1. an environment variable (comma-separated list for list values; use the TOML file if an item needs
     leading/trailing spaces and you prefer not to quote them in the shell), then
  2. an optional, untracked TOML file: $RESEARCH_TOOLKIT_CONFIG or
     ~/.config/research-toolkit/config.toml,

and default to an empty list when neither is set. Example config.toml:

    personal_keywords = ["tax", "payslip", "passport"]
    privacy_names = ["Firstname", "Lastname"]
    own_path_patterns = ["my-laptop-backup"]
"""
import os

CONFIG_PATH = os.environ.get(
    "RESEARCH_TOOLKIT_CONFIG",
    os.path.expanduser("~/.config/research-toolkit/config.toml"),
)

# config key -> environment variable
ENV_VARS = {
    "personal_keywords": "RESEARCH_TOOLKIT_PERSONAL_KEYWORDS",
    "privacy_names": "RESEARCH_TOOLKIT_PRIVACY_NAMES",
    "own_path_patterns": "RESEARCH_TOOLKIT_OWN_PATH_PATTERNS",
}


def _load_file(path=None):
    path = path or CONFIG_PATH
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, OSError, ValueError):
        return {}


def get_list(key, config_path=None):
    """Return a list of strings for `key` (empty list if unset)."""
    env = os.environ.get(ENV_VARS[key])
    if env is not None:
        # whitespace is kept: keywords such as "lic " rely on a trailing space
        return [x for x in env.split(",") if x.strip()]
    value = _load_file(config_path).get(key, [])
    return [str(x) for x in value] if isinstance(value, list) else []
