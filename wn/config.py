"""Load the wn config file from ``~/.config/wn/config.json``."""

import dataclasses
import json
import os
import tempfile
from typing import Optional, Tuple

from wn.backends.utm import DEFAULT_UTMCTL_PATH, DEFAULT_VM_NAME
from wn.errors import WnError
from wn.shares import CONFIG_DIRECTORY, Share, split_shares
from wn.text_input import DEFAULT_KEY_DELAY_MS

CONFIG_RELATIVE_PATH = os.path.join(CONFIG_DIRECTORY, "config.json")


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
	shares: Tuple[Share, ...] = ()


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
	"shares": (list,),
}
OBSOLETE_FIELDS = ("qmp_port", "qmp_session_port")
PATH_FIELDS = ("utmctl_path", "qmp_socket", "keygrab_binary", "keygrab_keycodes", "battery_file")
REMOVE_SETTING = object()


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
	if "shares" in values:
		problems = split_shares(path, values["shares"])[1]
		if problems:
			raise ConfigError(problems[0])


def _read_values(path):
	values = _read_json(path)
	if isinstance(values, dict):
		values = {key: value for key, value in values.items() if key not in OBSOLETE_FIELDS}
	return values


def _build(path, values):
	_validate(path, values)
	for field in PATH_FIELDS:
		if values.get(field):
			values[field] = os.path.expanduser(values[field])
	if "shares" in values:
		values["shares"] = tuple(Share(**item) for item in values["shares"])
	return Config(**values)


def load_config(path=None):
	"""Return the config, using defaults for anything the file does not set."""
	path = path or config_path()
	if not os.path.exists(path):
		return Config()
	return _build(path, _read_values(path))


def load_config_skipping_bad_shares(path=None):
	"""Return the config without its invalid shares, and a message for each share left out. Every other setting is checked as load_config checks it."""
	path = path or config_path()
	if not os.path.exists(path):
		return Config(), []
	values = _read_values(path)
	problems = []
	if isinstance(values, dict) and "shares" in values:
		if isinstance(values["shares"], list):
			values["shares"], problems = split_shares(path, values["shares"])
		else:
			del values["shares"]
			problems = ["{}: setting 'shares' has the wrong type".format(path)]
	return _build(path, values), problems


def update_config_file(values, path=None):
	"""Write ``values`` into the config file and keep every other setting. Create the file when it is missing. A value of REMOVE_SETTING removes that setting."""
	path = path or config_path()
	current = _read_json(path) if os.path.exists(path) else {}
	if not isinstance(current, dict):
		raise ConfigError("{} must contain a JSON object".format(path))
	for key, value in values.items():
		if value is REMOVE_SETTING:
			current.pop(key, None)
		else:
			current[key] = value
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
