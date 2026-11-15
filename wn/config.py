"""Load the wn config file from ``~/.config/wn/config.json``."""

import dataclasses
import json
import os
import tempfile
from typing import Optional

from wn.backends.utm import DEFAULT_UTMCTL_PATH, DEFAULT_VM_NAME
from wn.errors import WnError
from wn.text_input import DEFAULT_KEY_DELAY_MS

CONFIG_RELATIVE_PATH = os.path.join("~", ".config", "wn", "config.json")


class ConfigError(WnError):
	"""Raised when the config file cannot be read or has bad values."""


@dataclasses.dataclass(frozen=True)
class Config:
	vm_name: str = DEFAULT_VM_NAME
	backend: str = "utm"
	utmctl_path: str = DEFAULT_UTMCTL_PATH
	qmp_socket: Optional[str] = None
	key_delay_ms: int = DEFAULT_KEY_DELAY_MS
	keygrab_binary: Optional[str] = None
	keygrab_keycodes: Optional[str] = None
	keygrab_send_repeats: bool = True
	screen_size: Optional[str] = None
	battery_sync: bool = True
	battery_file: Optional[str] = None
	camera: bool = False


FIELD_TYPES = {
	"vm_name": (str,),
	"backend": (str,),
	"utmctl_path": (str,),
	"qmp_socket": (str, type(None)),
	"key_delay_ms": (int,),
	"keygrab_binary": (str, type(None)),
	"keygrab_keycodes": (str, type(None)),
	"keygrab_send_repeats": (bool,),
	"screen_size": (str, type(None)),
	"battery_sync": (bool,),
	"battery_file": (str, type(None)),
	"camera": (bool,),
}
OBSOLETE_FIELDS = ("qmp_port", "qmp_session_port")
PATH_FIELDS = ("utmctl_path", "qmp_socket", "keygrab_binary", "keygrab_keycodes", "battery_file")


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
		allowed_types = FIELD_TYPES[key]
		if not isinstance(value, allowed_types) or (isinstance(value, bool) and bool not in allowed_types):
			raise ConfigError("{}: setting '{}' has the wrong type".format(path, key))


def load_config(path=None):
	"""Return the config, using defaults for anything the file does not set."""
	path = path or config_path()
	if not os.path.exists(path):
		return Config()
	values = _read_json(path)
	if isinstance(values, dict):
		values = {key: value for key, value in values.items() if key not in OBSOLETE_FIELDS}
	_validate(path, values)
	for field in PATH_FIELDS:
		if values.get(field):
			values[field] = os.path.expanduser(values[field])
	return Config(**values)


def update_config_file(values, path=None):
	"""Write ``values`` into the config file and keep every other setting. Create the file when it is missing."""
	path = path or config_path()
	current = _read_json(path) if os.path.exists(path) else {}
	if not isinstance(current, dict):
		raise ConfigError("{} must contain a JSON object".format(path))
	current.update(values)
	directory = os.path.dirname(path)
	os.makedirs(directory, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".config-", suffix=".json", delete=False) as config_file:
		try:
			json.dump(current, config_file, indent="\t")
			config_file.write("\n")
		except BaseException:
			config_file.close()
			os.unlink(config_file.name)
			raise
	os.replace(config_file.name, path)
