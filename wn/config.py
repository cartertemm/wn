"""Load the wn config file from ``~/.config/wn/config.json``."""

import dataclasses
import json
import os
from typing import Optional

from wn.errors import WnError
from wn.text_input import DEFAULT_KEY_DELAY_MS

CONFIG_RELATIVE_PATH = os.path.join("~", ".config", "wn", "config.json")


class ConfigError(WnError):
	"""Raised when the config file cannot be read or has bad values."""


@dataclasses.dataclass(frozen=True)
class Config:
	vm_name: str = "Windows"
	backend: str = "utm"
	qmp_socket: Optional[str] = None
	key_delay_ms: int = DEFAULT_KEY_DELAY_MS


FIELD_TYPES = {
	"vm_name": (str,),
	"backend": (str,),
	"qmp_socket": (str, type(None)),
	"key_delay_ms": (int,),
}


def config_path():
	return os.path.expanduser(CONFIG_RELATIVE_PATH)


def _read_json(path):
	try:
		with open(path) as config_file:
			return json.load(config_file)
	except json.JSONDecodeError as error:
		raise ConfigError("{} is not valid JSON: {}".format(path, error)) from None
	except OSError as error:
		raise ConfigError("cannot read {}: {}".format(path, error)) from None


def _validate(path, values):
	if not isinstance(values, dict):
		raise ConfigError("{} must contain a JSON object".format(path))
	for key, value in values.items():
		if key not in FIELD_TYPES:
			raise ConfigError("{}: unknown setting '{}'. Known settings: {}".format(path, key, ", ".join(FIELD_TYPES)))
		if not isinstance(value, FIELD_TYPES[key]) or isinstance(value, bool):
			raise ConfigError("{}: setting '{}' has the wrong type".format(path, key))


def load_config(path=None):
	"""Return the config, using defaults for anything the file does not set."""
	path = path or config_path()
	if not os.path.exists(path):
		return Config()
	values = _read_json(path)
	_validate(path, values)
	if values.get("qmp_socket"):
		values["qmp_socket"] = os.path.expanduser(values["qmp_socket"])
	return Config(**values)
