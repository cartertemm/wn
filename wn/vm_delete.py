"""Find and remove everything wn made on the Mac for one VM."""

import dataclasses
import functools
import os
import shutil
from typing import Callable

from wn.agent import LOG_PATH, launch_agent_path, uninstall_launch_agent
from wn.battery_file import battery_file_path
from wn.config import REMOVE_SETTING, config_path, update_config_file
from wn.errors import WnError
from wn.share_sftp import remove_file, remove_host_key, share_paths
from wn.shares import SUPPORT_DIRECTORY
from wn.utm_config import camera_socket_file, default_battery_file

EXIT_SUCCESS = 0
EXIT_FAILURE = 1


@dataclasses.dataclass(frozen=True)
class DeleteItem:
	label: str
	remove: Callable[[], object]


def plan_delete(name, config, backend, in_utm, include_all=False):
	"""Return the items to remove for VM ``name``, in removal order. Only things that exist are listed.

	With ``include_all``, also remove the launch agent and the whole wn support folder."""
	active = name == config.vm_name
	shares = [share for share in config.shares if share.vm == name]
	items = []
	if (active or include_all) and os.path.exists(launch_agent_path()):
		items.append(DeleteItem("the wn agent launch agent, {}, and its log {}".format(launch_agent_path(), os.path.expanduser(LOG_PATH)), _uninstall_agent))
	if in_utm:
		items.append(DeleteItem("the VM {} in UTM, with its disk".format(name), backend.delete))
	battery_file = battery_file_path(config) if active else default_battery_file(name)
	for label, path in (("the battery file", battery_file), ("the camera socket", camera_socket_file(name))):
		if os.path.exists(path):
			items.append(DeleteItem("{} {}".format(label, path), functools.partial(remove_file, path)))
	paths = share_paths()
	for share in shares:
		items.append(DeleteItem("the host keys and the log of share {}".format(share.name), functools.partial(_remove_share_files, paths, share.name)))
	support = os.path.expanduser(SUPPORT_DIRECTORY)
	if include_all and os.path.exists(support):
		items.append(DeleteItem("the folder {} with the download cache, the share files, and wn-agent.app".format(support), functools.partial(shutil.rmtree, support)))
	if os.path.exists(config_path()) and (shares or active):
		items.append(DeleteItem("the settings for {} in {}".format(name, config_path()), functools.partial(_update_config, config, name, active)))
	return items


def run_delete(items, output, errors):
	"""Remove each item in order. Report a failure and go on with the rest. Return the exit code."""
	failed = False
	for item in items:
		try:
			item.remove()
		except (OSError, WnError) as error:
			errors.write("wn: error: cannot remove {}: {}\n".format(item.label, error))
			failed = True
			continue
		output.write("Removed {}\n".format(item.label))
	if failed:
		errors.write("wn: error: some items were not removed. Fix the errors above, then run wn delete again\n")
		return EXIT_FAILURE
	return EXIT_SUCCESS


def _uninstall_agent():
	uninstall_launch_agent()
	remove_file(os.path.expanduser(LOG_PATH))


def _remove_share_files(paths, name):
	remove_host_key(paths, name)
	remove_file(paths.log_file(name))


def _update_config(config, name, active):
	kept = [share.to_dict() for share in config.shares if share.vm != name]
	values = {"shares": kept or REMOVE_SETTING}
	if active:
		values["vm_name"] = REMOVE_SETTING
	update_config_file(values)
