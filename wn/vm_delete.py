"""Find and remove everything wn made on the Mac for one VM."""

import dataclasses
import functools
import os
from typing import Callable

from wn.agent import launch_agent_path, uninstall_launch_agent
from wn.battery_file import battery_file_path
from wn.config import REMOVE_SETTING, config_path, update_config_file
from wn.share_sftp import remove_file, remove_host_key, share_paths
from wn.utm_config import camera_socket_file, default_battery_file


@dataclasses.dataclass(frozen=True)
class DeleteItem:
	label: str
	remove: Callable[[], object]


def plan_delete(name, config, backend, in_utm):
	"""Return the items to remove for VM ``name``, in removal order. Only things that exist are listed."""
	active = name == config.vm_name
	shares = [share for share in config.shares if share.vm == name]
	items = []
	if active and os.path.exists(launch_agent_path()):
		items.append(DeleteItem("the wn agent launch agent, {}".format(launch_agent_path()), uninstall_launch_agent))
	if in_utm:
		items.append(DeleteItem("the VM {} in UTM, with its disk".format(name), backend.delete))
	battery_file = battery_file_path(config) if active else default_battery_file(name)
	for label, path in (("the battery file", battery_file), ("the camera socket", camera_socket_file(name))):
		if os.path.exists(path):
			items.append(DeleteItem("{} {}".format(label, path), functools.partial(remove_file, path)))
	paths = share_paths()
	for share in shares:
		items.append(DeleteItem("the host keys of share {}".format(share.name), functools.partial(remove_host_key, paths, share.name)))
	if os.path.exists(config_path()) and (shares or active):
		items.append(DeleteItem("the settings for {} in {}".format(name, config_path()), functools.partial(_update_config, config, name, active)))
	return items


def _update_config(config, name, active):
	kept = [share.to_dict() for share in config.shares if share.vm != name]
	values = {"shares": kept or REMOVE_SETTING}
	if active:
		values["vm_name"] = REMOVE_SETTING
	update_config_file(values)
