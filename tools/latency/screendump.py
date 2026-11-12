"""Save a PNG of the guest screen: python3 tools/latency/screendump.py OUT.png

QEMU under UTM can only write screendumps to a relative path, which lands in
UTM's group container, and this QEMU has no PNG support. This converts the
PPM file to OUT.png with sips.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wn.qmp import qmp_client_for

QMP_ADDRESS = "tcp:127.0.0.1:4444"
GROUP_CONTAINER = os.path.expanduser("~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM")
TEMPORARY_NAME = "wn-latency-screendump.ppm"


def screendump(output_path, address=QMP_ADDRESS):
	with qmp_client_for(address) as client:
		client.execute("screendump", {"filename": TEMPORARY_NAME})
	ppm_path = os.path.join(GROUP_CONTAINER, TEMPORARY_NAME)
	subprocess.run(["sips", "-s", "format", "png", ppm_path, "--out", output_path], check=True, capture_output=True)
	os.remove(ppm_path)


if __name__ == "__main__":
	screendump(sys.argv[1])
