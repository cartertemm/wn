import hashlib
import http.client
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.response
import zipfile
from unittest import mock

from wn import downloads
from wn.downloads import (
	CACHE_FILE_NAME,
	DownloadError,
	Downloads,
	ShareTools,
	ZipMember,
	extract_verified_member,
	fetch_all,
	fetch_share_tools,
	parse_github_digest,
	parse_nvda_version,
	parse_product_edition_id,
	parse_windows_english_hash,
	pick_arm64_link,
	pick_english_sku,
)
from wn.errors import WnError

WINDOWS_ISO_NAME = "Windows11_Client_arm64_en-us_26300_9457.iso"
WINDOWS_ISO_URL = "https://software.download.prss.microsoft.com/dbazure/" + WINDOWS_ISO_NAME + "?t=token&P1=1"
VIRTIO_ISO_NAME = "virtio-win-0.1.302.iso"
VIRTIO_ISO_URL = "https://github.com/qemus/virtiso-arm/releases/download/v0.1.302-1/" + VIRTIO_ISO_NAME
GUEST_TOOLS_TAG = "v10.0.2-utm"
GUEST_TOOLS_NAME = "utm-guest-tools-0.1.271.iso"
GUEST_TOOLS_URL = "https://github.com/utmapp/qemu/releases/download/" + GUEST_TOOLS_TAG + "/" + GUEST_TOOLS_NAME
NVDA_VERSION = "2026.3.1"
NVDA_NAME = "nvda_" + NVDA_VERSION + ".exe"
NVDA_URL = "https://download.nvaccess.org/releases/" + NVDA_VERSION + "/" + NVDA_NAME

WINDOWS_ISO_DATA = b"w" * 400000
VIRTIO_ISO_DATA = b"virtio" * 3000
GUEST_TOOLS_DATA = b"guest tools" * 2000
NVDA_DATA = b"nvda" * 4000


def sha256_of(data):
	return hashlib.sha256(data).hexdigest()


def windows_page(english_hash=None):
	english_hash = english_hash or sha256_of(WINDOWS_ISO_DATA)
	return (
		'<select id="product-edition"><option value="null" selected="selected">Select Download</option>'
		'<option value="3816">Windows 11 (multi-edition ISO for Arm64) </option></select>'
		'<table class="table table-bordered"><caption>Hash values for the ISO files for Each Language</caption><tbody>'
		'<tr><th>Country Locale</th><th>Hash Code</th></tr>'
		'<tr><td>Arabic  64-bit</td><td>92775CAD76E5B90B6BC0D2F7ADA982B5E399CBB59BFA797320872016927848DD</td></tr>'
		'<tr><td>English International  64-bit</td><td>500BCB6F272B7C455156470EA06816F558F13B885723F84EE9EAB7534C7DEE61</td></tr>'
		'<tr><td>English  64-bit</td><td>' + english_hash.upper() + '</td></tr>'
		'</tbody></table>'
	)


MDT_JS = (
	'window.dfp={url:"https://ov-df.microsoft.com/?session_id=s&CustomerId=c&PageId=si&w=8DF3FBE60BC6866",sessionId:"s"};'
	'src+="&mdt="+start;src+="&rticks="+1794379100919;'
)

SKU_RESPONSE = {
	"ValidationContainer": {"Errors": []},
	"Skus": [
		{"Id": "27163", "Language": "Arabic", "LocalizedLanguage": "Arabic"},
		{"Id": "27169", "Language": "English (United Kingdom)", "LocalizedLanguage": "English International"},
		{"Id": "27162", "Language": "English", "LocalizedLanguage": "English (United States)"},
	],
}

LINKS_RESPONSE = {
	"ProductDownloadOptions": [
		{"Name": "Windows 11 x64", "Uri": "https://example.invalid/x64.iso", "DownloadType": 1},
		{"Name": "Windows 11 ARM 64 26H2 26300.9457 English", "Uri": WINDOWS_ISO_URL, "DownloadType": 2},
	],
	"ValidationContainer": {"ErrorList": [], "Errors": []},
}

SENTINEL_REJECT = {"Errors": [{"Key": "ErrorSettings.SentinelReject", "Value": "Sentinel marked this request as rejected.", "Type": 8}]}

NVDA_PAGE = '<a href="https://www.nvaccess.org/download/nvda/releases/' + NVDA_VERSION + '/' + NVDA_NAME + '">Download</a>'


def github_asset(name, data, url, digest=None):
	return {"name": name, "size": len(data), "digest": digest or "sha256:" + sha256_of(data), "browser_download_url": url}


def virtio_release():
	return {
		"tag_name": "v0.1.302-1",
		"assets": [
			github_asset("virtio-win-0.1.302.tar.gz", b"tarball", "https://example.invalid/tar.gz"),
			github_asset(VIRTIO_ISO_NAME, VIRTIO_ISO_DATA, VIRTIO_ISO_URL),
			github_asset("virtio-win.iso", VIRTIO_ISO_DATA, "https://example.invalid/virtio-win.iso"),
		],
	}


def guest_tools_release():
	return {"tag_name": GUEST_TOOLS_TAG, "assets": [github_asset(GUEST_TOOLS_NAME, GUEST_TOOLS_DATA, GUEST_TOOLS_URL)]}


def nvda_release(body_hash=None):
	body_hash = body_hash or sha256_of(NVDA_DATA)
	return {
		"tag_name": "release-" + NVDA_VERSION,
		"body": "* Highlights.\n* SHA256 sum: `" + body_hash + "`\n",
		"assets": [github_asset(NVDA_NAME, NVDA_DATA, "https://github.com/nvaccess/nvda/releases/download/release-" + NVDA_VERSION + "/" + NVDA_NAME)],
	}


def make_response(url, body, code=200, headers=None):
	message = http.client.HTTPMessage()
	message["Content-Length"] = str(len(body))
	for name, value in (headers or {}).items():
		message[name] = value
	return urllib.response.addinfourl(io.BytesIO(body), message, url, code)


class FakeOpener:
	"""Answers requests from a table keyed by URL without its query string."""

	def __init__(self):
		self.requests = []
		self.routes = {
			downloads.WINDOWS_PAGE_URL: windows_page().encode("utf-8"),
			downloads.MICROSOFT_TAGS_URL: b"",
			downloads.MICROSOFT_MDT_URL: MDT_JS.encode("utf-8"),
			downloads.MICROSOFT_FINGERPRINT_URL: b"",
			downloads.MICROSOFT_SKU_URL: json.dumps(SKU_RESPONSE).encode("utf-8"),
			downloads.MICROSOFT_LINKS_URL: json.dumps(LINKS_RESPONSE).encode("utf-8"),
			WINDOWS_ISO_URL.split("?")[0]: WINDOWS_ISO_DATA,
			downloads.VIRTIO_RELEASE_API_URL: json.dumps(virtio_release()).encode("utf-8"),
			VIRTIO_ISO_URL: VIRTIO_ISO_DATA,
			downloads.GUEST_TOOLS_LATEST_URL: ("redirect", GUEST_TOOLS_URL),
			"https://api.github.com/repos/utmapp/qemu/releases/tags/" + GUEST_TOOLS_TAG: json.dumps(guest_tools_release()).encode("utf-8"),
			GUEST_TOOLS_URL: ("redirect", "https://objects.githubusercontent.com/guest-tools-blob"),
			"https://objects.githubusercontent.com/guest-tools-blob": GUEST_TOOLS_DATA,
			downloads.NVDA_DOWNLOAD_PAGE_URL: NVDA_PAGE.encode("utf-8"),
			"https://api.github.com/repos/nvaccess/nvda/releases/tags/release-" + NVDA_VERSION: json.dumps(nvda_release()).encode("utf-8"),
			NVDA_URL: NVDA_DATA,
		}

	def set_json(self, url, value):
		self.routes[url] = json.dumps(value).encode("utf-8")

	def requested(self, url):
		return [requested for requested in self.requests if requested.split("?")[0] == url]

	def open(self, request, timeout=None):
		url = request.full_url
		self.requests.append(url)
		route = self.routes.get(url.split("?")[0])
		if route is None:
			raise urllib.error.URLError("no route for " + url)
		if isinstance(route, Exception):
			raise route
		if isinstance(route, tuple):
			message = http.client.HTTPMessage()
			message["Location"] = route[1]
			raise urllib.error.HTTPError(url, 302, "Found", message, io.BytesIO(b""))
		return make_response(url, route)


class ParserTests(unittest.TestCase):
	def test_product_edition_id_comes_from_the_arm64_option(self):
		self.assertEqual(parse_product_edition_id(windows_page()), "3816")

	def test_product_edition_id_missing_raises(self):
		with self.assertRaises(DownloadError):
			parse_product_edition_id("<html>nothing here</html>")

	def test_english_hash_skips_english_international(self):
		self.assertEqual(parse_windows_english_hash(windows_page("AB" * 32)), "ab" * 32)

	def test_english_hash_missing_raises(self):
		with self.assertRaises(DownloadError):
			parse_windows_english_hash("<table><tr><td>Arabic 64-bit</td><td>" + "A" * 64 + "</td></tr></table>")

	def test_english_sku_is_united_states_english(self):
		self.assertEqual(pick_english_sku(SKU_RESPONSE), "27162")

	def test_english_sku_missing_raises(self):
		with self.assertRaises(DownloadError):
			pick_english_sku({"Skus": [SKU_RESPONSE["Skus"][1]]})

	def test_arm64_link_is_download_type_two(self):
		self.assertEqual(pick_arm64_link(LINKS_RESPONSE), WINDOWS_ISO_URL)

	def test_arm64_link_missing_raises(self):
		with self.assertRaises(DownloadError):
			pick_arm64_link({"ProductDownloadOptions": [LINKS_RESPONSE["ProductDownloadOptions"][0]]})

	def test_sentinel_reject_raises_ban_message(self):
		with self.assertRaises(DownloadError) as caught:
			pick_arm64_link(SENTINEL_REJECT)
		self.assertIn("715-123130", str(caught.exception))
		self.assertIn("Sentinel marked this request as rejected", str(caught.exception))

	def test_github_digest_is_lowercase_hex(self):
		self.assertEqual(parse_github_digest({"name": "a.iso", "digest": "sha256:" + "AB" * 32}), "ab" * 32)

	def test_github_digest_missing_raises(self):
		for asset in ({"name": "a.iso"}, {"name": "a.iso", "digest": None}, {"name": "a.iso", "digest": "md5:abc"}):
			with self.assertRaises(DownloadError):
				parse_github_digest(asset)

	def test_nvda_version_from_download_page(self):
		self.assertEqual(parse_nvda_version(NVDA_PAGE), NVDA_VERSION)

	def test_nvda_version_from_direct_download_link(self):
		self.assertEqual(parse_nvda_version('<a href="' + NVDA_URL + '">'), NVDA_VERSION)

	def test_nvda_version_missing_raises(self):
		with self.assertRaises(DownloadError):
			parse_nvda_version("<html>no links</html>")

	def test_download_error_is_wn_error(self):
		self.assertTrue(issubclass(DownloadError, WnError))


class FetchAllTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.downloads_dir = os.path.join(self.directory.name, "downloads")
		self.opener = FakeOpener()
		self.lines = []

	def fetch(self, **options):
		options.setdefault("report", self.lines.append)
		options.setdefault("opener", self.opener)
		return fetch_all(self.downloads_dir, **options)

	def path(self, name):
		return os.path.join(self.downloads_dir, name)

	def read(self, name):
		with open(self.path(name), "rb") as file:
			return file.read()

	def write(self, name, data):
		os.makedirs(self.downloads_dir, exist_ok=True)
		with open(self.path(name), "wb") as file:
			file.write(data)

	def test_downloads_and_verifies_all_four_files(self):
		result = self.fetch()
		self.assertEqual(result, Downloads(
			windows_iso=self.path(WINDOWS_ISO_NAME),
			virtio_iso=self.path(VIRTIO_ISO_NAME),
			guest_tools_iso=self.path(GUEST_TOOLS_NAME),
			nvda_installer=self.path(NVDA_NAME),
		))
		self.assertEqual(self.read(WINDOWS_ISO_NAME), WINDOWS_ISO_DATA)
		self.assertEqual(self.read(VIRTIO_ISO_NAME), VIRTIO_ISO_DATA)
		self.assertEqual(self.read(GUEST_TOOLS_NAME), GUEST_TOOLS_DATA)
		self.assertEqual(self.read(NVDA_NAME), NVDA_DATA)
		self.assertEqual([name for name in os.listdir(self.downloads_dir) if name.endswith(".part")], [])
		self.assertIn("Windows ISO: 100% (0.4 of 0.4 MB)", self.lines)

	def test_microsoft_flow_registers_session_and_reuses_it(self):
		self.fetch(include_nvda=False)
		tags = self.opener.requested(downloads.MICROSOFT_TAGS_URL)
		skus = self.opener.requested(downloads.MICROSOFT_SKU_URL)
		links = self.opener.requested(downloads.MICROSOFT_LINKS_URL)
		fingerprint = self.opener.requested(downloads.MICROSOFT_FINGERPRINT_URL)
		self.assertEqual((len(tags), len(skus), len(links), len(fingerprint)), (1, 1, 1, 1))
		session_id = urllib.parse.parse_qs(urllib.parse.urlsplit(tags[0]).query)["session_id"][0]
		self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(skus[0]).query)["productEditionId"], ["3816"])
		self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(skus[0]).query)["sessionID"], [session_id])
		self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(links[0]).query)["SKU"], ["27162"])
		self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(links[0]).query)["sessionID"], [session_id])
		fingerprint_query = urllib.parse.parse_qs(urllib.parse.urlsplit(fingerprint[0]).query)
		self.assertEqual(fingerprint_query["w"], ["8DF3FBE60BC6866"])
		self.assertEqual(fingerprint_query["rticks"], ["1794379100919"])

	def test_requests_send_a_browser_user_agent(self):
		seen = []
		original_open = self.opener.open

		def recording_open(request, timeout=None):
			seen.append(request.get_header("User-agent"))
			return original_open(request, timeout)

		self.fetch(include_nvda=False, opener=recording_open)
		self.assertTrue(seen)
		self.assertTrue(all(agent == downloads.USER_AGENT for agent in seen))

	def test_without_nvda_skips_nvda(self):
		result = self.fetch(include_nvda=False)
		self.assertIsNone(result.nvda_installer)
		self.assertEqual(self.opener.requested(downloads.NVDA_DOWNLOAD_PAGE_URL), [])
		self.assertFalse(os.path.exists(self.path(NVDA_NAME)))

	def test_second_run_reports_already_verified_and_downloads_nothing(self):
		self.fetch()
		self.lines.clear()
		del self.opener.routes[WINDOWS_ISO_URL.split("?")[0]]
		del self.opener.routes[VIRTIO_ISO_URL]
		del self.opener.routes["https://objects.githubusercontent.com/guest-tools-blob"]
		del self.opener.routes[NVDA_URL]
		self.fetch()
		self.assertEqual(self.lines, [
			"Windows ISO: already downloaded and verified",
			"VirtIO drivers ISO: already downloaded and verified",
			"UTM guest tools ISO: already downloaded and verified",
			"NVDA installer: already downloaded and verified",
		])

	def test_verified_windows_iso_matching_page_hash_skips_download_api(self):
		self.fetch(include_nvda=False)
		self.opener.requests.clear()
		self.opener.set_json(downloads.MICROSOFT_LINKS_URL, SENTINEL_REJECT)
		self.lines.clear()
		result = self.fetch(include_nvda=False)
		self.assertEqual(result.windows_iso, self.path(WINDOWS_ISO_NAME))
		self.assertEqual(self.lines[0], "Windows ISO: already downloaded and verified")
		self.assertEqual(self.opener.requested(downloads.MICROSOFT_TAGS_URL), [])
		self.assertEqual(self.opener.requested(downloads.MICROSOFT_SKU_URL), [])
		self.assertEqual(self.opener.requested(downloads.MICROSOFT_LINKS_URL), [])

	def test_cache_is_trusted_when_size_and_mtime_match(self):
		self.fetch(include_nvda=False)
		path = self.path(VIRTIO_ISO_NAME)
		status = os.stat(path)
		self.write(VIRTIO_ISO_NAME, b"X" * len(VIRTIO_ISO_DATA))
		os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
		del self.opener.routes[VIRTIO_ISO_URL]
		self.fetch(include_nvda=False)
		self.assertEqual(self.read(VIRTIO_ISO_NAME), b"X" * len(VIRTIO_ISO_DATA))

	def test_cache_records_path_size_mtime_and_hash(self):
		self.fetch(include_nvda=False)
		with open(self.path(CACHE_FILE_NAME), encoding="utf-8") as file:
			cache = json.load(file)
		entry = cache[VIRTIO_ISO_NAME]
		status = os.stat(self.path(VIRTIO_ISO_NAME))
		self.assertEqual(entry["path"], self.path(VIRTIO_ISO_NAME))
		self.assertEqual(entry["size"], status.st_size)
		self.assertEqual(entry["mtime_ns"], status.st_mtime_ns)
		self.assertEqual(entry["sha256"], sha256_of(VIRTIO_ISO_DATA))

	def test_changed_mtime_rehashes_and_redownloads_bad_file(self):
		self.fetch(include_nvda=False)
		self.write(VIRTIO_ISO_NAME, b"X" * len(VIRTIO_ISO_DATA))
		os.utime(self.path(VIRTIO_ISO_NAME), (1, 1))
		self.fetch(include_nvda=False)
		self.assertEqual(self.read(VIRTIO_ISO_NAME), VIRTIO_ISO_DATA)

	def test_existing_file_with_right_hash_is_not_downloaded(self):
		self.write(VIRTIO_ISO_NAME, VIRTIO_ISO_DATA)
		del self.opener.routes[VIRTIO_ISO_URL]
		self.fetch(include_nvda=False)
		self.assertIn("VirtIO drivers ISO: already downloaded and verified", self.lines)

	def test_hash_mismatch_deletes_part_file_and_raises(self):
		self.opener.routes[VIRTIO_ISO_URL] = b"tampered" * 10
		with self.assertRaises(DownloadError) as caught:
			self.fetch(include_nvda=False)
		self.assertIn("SHA256", str(caught.exception))
		self.assertEqual([name for name in os.listdir(self.downloads_dir) if VIRTIO_ISO_NAME in name], [])

	def test_windows_hash_mismatch_deletes_part_file(self):
		self.opener.routes[downloads.WINDOWS_PAGE_URL] = windows_page("CD" * 32).encode("utf-8")
		with self.assertRaises(DownloadError):
			self.fetch(include_nvda=False)
		self.assertEqual([name for name in os.listdir(self.downloads_dir) if WINDOWS_ISO_NAME in name], [])

	def test_guest_tools_saved_under_versioned_name_from_redirect(self):
		result = self.fetch(include_nvda=False)
		self.assertEqual(os.path.basename(result.guest_tools_iso), GUEST_TOOLS_NAME)

	def test_nvda_release_hash_must_match_asset_digest(self):
		self.opener.set_json("https://api.github.com/repos/nvaccess/nvda/releases/tags/release-" + NVDA_VERSION, nvda_release("ef" * 32))
		with self.assertRaises(DownloadError):
			self.fetch()

	def test_unsafe_file_name_from_microsoft_raises(self):
		unsafe = dict(LINKS_RESPONSE["ProductDownloadOptions"][1], Uri="https://software.download.prss.microsoft.com/dbazure/..%2F..%2Fevil.iso")
		self.opener.set_json(downloads.MICROSOFT_LINKS_URL, {"ProductDownloadOptions": [unsafe]})
		with self.assertRaises(DownloadError):
			self.fetch(include_nvda=False)
		self.assertFalse(os.path.exists(os.path.join(self.directory.name, "evil.iso")))

	def test_microsoft_ban_raises_clear_error(self):
		self.opener.set_json(downloads.MICROSOFT_LINKS_URL, SENTINEL_REJECT)
		with self.assertRaises(DownloadError) as caught:
			self.fetch(include_nvda=False)
		self.assertIn("715-123130", str(caught.exception))

	def test_microsoft_html_ban_page_raises_clear_error(self):
		self.opener.routes[downloads.MICROSOFT_LINKS_URL] = b"<html>We are unable to complete your request at this time. 715-123130</html>"
		with self.assertRaises(DownloadError) as caught:
			self.fetch(include_nvda=False)
		self.assertIn("715-123130", str(caught.exception))

	def test_microsoft_failure_uses_previously_verified_windows_iso(self):
		self.fetch(include_nvda=False)
		self.lines.clear()
		self.opener.routes[downloads.WINDOWS_PAGE_URL] = windows_page("CD" * 32).encode("utf-8")
		self.opener.set_json(downloads.MICROSOFT_LINKS_URL, SENTINEL_REJECT)
		result = self.fetch(include_nvda=False)
		self.assertEqual(result.windows_iso, self.path(WINDOWS_ISO_NAME))
		self.assertTrue(self.lines[0].startswith("Windows ISO: could not check Microsoft"))
		self.assertIn(WINDOWS_ISO_NAME, self.lines[0])

	def test_network_failure_uses_previously_verified_windows_iso(self):
		self.fetch(include_nvda=False)
		self.opener.routes[downloads.WINDOWS_PAGE_URL] = urllib.error.URLError("offline")
		result = self.fetch(include_nvda=False)
		self.assertEqual(result.windows_iso, self.path(WINDOWS_ISO_NAME))

	def test_microsoft_failure_without_cache_raises(self):
		self.opener.routes[downloads.WINDOWS_PAGE_URL] = urllib.error.URLError("offline")
		with self.assertRaises(DownloadError):
			self.fetch(include_nvda=False)

	def test_microsoft_failure_ignores_cached_iso_that_changed(self):
		self.fetch(include_nvda=False)
		os.utime(self.path(WINDOWS_ISO_NAME), (1, 1))
		self.opener.routes[downloads.WINDOWS_PAGE_URL] = urllib.error.URLError("offline")
		with self.assertRaises(DownloadError):
			self.fetch(include_nvda=False)

	def test_default_directory_is_application_support(self):
		self.assertEqual(downloads.DEFAULT_DOWNLOADS_DIR, os.path.join("~", "Library", "Application Support", "wn", "downloads"))


class ProgressTests(unittest.TestCase):
	def test_progress_lines_are_throttled_by_percent(self):
		lines = []
		progress = downloads.Progress("Windows ISO", 8500 * downloads.BYTES_PER_MB, lines.append, clock=lambda: 0)
		for step in range(1, 1001):
			progress.update(step * 8500 * downloads.BYTES_PER_MB // 1000)
		self.assertEqual(len(lines), 100 // downloads.PROGRESS_PERCENT_STEP)
		self.assertEqual(lines[-1], "Windows ISO: 100% (8.5 of 8.5 GB)")

	def test_progress_reports_after_interval_even_without_percent_step(self):
		lines = []
		now = [0]
		progress = downloads.Progress("Windows ISO", 8500 * downloads.BYTES_PER_MB, lines.append, clock=lambda: now[0])
		now[0] = downloads.PROGRESS_INTERVAL_SECONDS
		progress.update(3600 * downloads.BYTES_PER_MB // 100)
		self.assertEqual(lines, ["Windows ISO: 0% (0.0 of 8.5 GB)"])

	def test_progress_without_size(self):
		lines = []
		now = [0]
		progress = downloads.Progress("NVDA installer", None, lines.append, clock=lambda: now[0])
		now[0] = downloads.PROGRESS_INTERVAL_SECONDS
		progress.update(42 * downloads.BYTES_PER_MB)
		self.assertEqual(lines, ["NVDA installer: 42.0 MB"])


RCLONE_MAC_URL = "https://downloads.example.invalid/rclone-mac.zip"
RCLONE_WINDOWS_URL = "https://downloads.example.invalid/rclone-windows.zip"
WINFSP_URL = "https://github.com/example/winfsp.msi"
RCLONE_MAC_NAME = "rclone-mac.zip"
RCLONE_WINDOWS_NAME = "rclone-windows.zip"
WINFSP_NAME = "winfsp.msi"
RCLONE_MAC_DATA = b"rclone mac" * 3000
RCLONE_WINDOWS_DATA = b"rclone windows" * 3000
WINFSP_DATA = b"winfsp" * 3000
MEMBER_NAME = "folder/rclone"
MEMBER_DATA = b"the rclone binary" * 1000
EXECUTABLE_MODE = 0o755
WRONG_MODE = 0o600
OLD_TIME_NS = 1000000000


class ShareToolsTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.downloads_dir = os.path.join(self.directory.name, "downloads")
		self.opener = FakeOpener()
		self.opener.routes[RCLONE_MAC_URL] = RCLONE_MAC_DATA
		self.opener.routes[RCLONE_WINDOWS_URL] = RCLONE_WINDOWS_DATA
		self.opener.routes[WINFSP_URL] = WINFSP_DATA
		self.lines = []

	def patch_sources(self):
		for name, source in (
			("RCLONE_MAC_SOURCE", downloads._Source(RCLONE_MAC_URL, RCLONE_MAC_NAME, sha256_of(RCLONE_MAC_DATA))),
			("RCLONE_WINDOWS_SOURCE", downloads._Source(RCLONE_WINDOWS_URL, RCLONE_WINDOWS_NAME, sha256_of(RCLONE_WINDOWS_DATA))),
			("WINFSP_SOURCE", downloads._Source(WINFSP_URL, WINFSP_NAME, sha256_of(WINFSP_DATA))),
		):
			patcher = mock.patch.object(downloads, name, source)
			patcher.start()
			self.addCleanup(patcher.stop)

	def fetch(self):
		return fetch_share_tools(self.downloads_dir, report=self.lines.append, opener=self.opener)

	def path(self, name):
		return os.path.join(self.downloads_dir, name)

	def make_zip(self):
		zip_path = os.path.join(self.directory.name, "tools.zip")
		with zipfile.ZipFile(zip_path, "w") as archive:
			archive.writestr("folder/rclone.1", b"manual page")
			archive.writestr(MEMBER_NAME, MEMBER_DATA)
			archive.writestr("rclone", b"top level decoy")
		return zip_path

	def destination(self):
		return os.path.join(self.directory.name, "bin", "rclone")

	def test_pinned_sources_use_the_published_hashes(self):
		self.assertEqual(downloads.RCLONE_MAC_SOURCE, downloads._Source(
			"https://downloads.rclone.org/v1.76.0/rclone-v1.76.0-osx-arm64.zip",
			"rclone-v1.76.0-osx-arm64.zip",
			"e72dc90b70f4947a7b72b61bde4a4fbba5777b2874fb448951ffdde67ebd0ab7",
		))
		self.assertEqual(downloads.RCLONE_WINDOWS_SOURCE, downloads._Source(
			"https://downloads.rclone.org/v1.76.0/rclone-v1.76.0-windows-arm64.zip",
			"rclone-v1.76.0-windows-arm64.zip",
			"c406c8a69a5a408e50624aa990fbb0a6266b3a4e83782a94de3cb96f50f86f10",
		))
		self.assertEqual(downloads.WINFSP_SOURCE, downloads._Source(
			"https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi",
			"winfsp-2.1.25156.msi",
			"073a70e00f77423e34bed98b86e600def93393ba5822204fac57a29324db9f7a",
		))
		self.assertEqual(downloads.RCLONE_MAC_BINARY, ZipMember("rclone-v1.76.0-osx-arm64/rclone", "da7c43d491e41966f4af7f5f4ac34f7dac2c8866e3675393ef0316ebc57ccd42"))
		self.assertEqual(downloads.RCLONE_WINDOWS_BINARY, ZipMember("rclone-v1.76.0-windows-arm64/rclone.exe", "666b57912d75ecc67b38960c4e28448e94b61d7977f5f709e25c0c7a3c6d7fcd"))

	def test_fetch_share_tools_downloads_and_verifies_three_files(self):
		self.patch_sources()
		result = self.fetch()
		self.assertEqual(result, ShareTools(self.path(RCLONE_MAC_NAME), self.path(RCLONE_WINDOWS_NAME), self.path(WINFSP_NAME)))
		for name, data in ((RCLONE_MAC_NAME, RCLONE_MAC_DATA), (RCLONE_WINDOWS_NAME, RCLONE_WINDOWS_DATA), (WINFSP_NAME, WINFSP_DATA)):
			with open(self.path(name), "rb") as file:
				self.assertEqual(file.read(), data)
		with open(self.path(CACHE_FILE_NAME), encoding="utf-8") as file:
			cache = json.load(file)
		self.assertEqual(sorted(entry["role"] for entry in cache.values()), sorted([downloads.RCLONE_MAC_ROLE, downloads.RCLONE_WINDOWS_ROLE, downloads.WINFSP_ROLE]))

	def test_second_run_downloads_nothing(self):
		self.patch_sources()
		self.fetch()
		self.opener.requests.clear()
		self.lines.clear()
		self.fetch()
		self.assertEqual(self.opener.requests, [])
		self.assertEqual(self.lines, [
			"rclone for macOS: already downloaded and verified",
			"rclone for Windows: already downloaded and verified",
			"WinFsp installer: already downloaded and verified",
		])

	def test_wrong_file_is_deleted(self):
		self.patch_sources()
		self.opener.routes[RCLONE_MAC_URL] = b"tampered" * 100
		with self.assertRaises(DownloadError) as caught:
			self.fetch()
		self.assertIn("SHA256", str(caught.exception))
		self.assertFalse(os.path.exists(self.path(RCLONE_MAC_NAME)))
		self.assertFalse(os.path.exists(self.path(RCLONE_MAC_NAME) + downloads.PART_SUFFIX))

	def test_extract_takes_only_the_exact_member(self):
		destination = self.destination()
		result = extract_verified_member(self.make_zip(), ZipMember(MEMBER_NAME, sha256_of(MEMBER_DATA)), destination, EXECUTABLE_MODE)
		self.assertEqual(result, destination)
		with open(destination, "rb") as file:
			self.assertEqual(file.read(), MEMBER_DATA)
		self.assertEqual(stat.S_IMODE(os.stat(destination).st_mode), EXECUTABLE_MODE)
		self.assertEqual(os.listdir(os.path.dirname(destination)), ["rclone"])

	def test_extract_with_a_wrong_hash_deletes_the_file(self):
		destination = self.destination()
		with self.assertRaises(DownloadError) as caught:
			extract_verified_member(self.make_zip(), ZipMember(MEMBER_NAME, "0" * 64), destination, EXECUTABLE_MODE)
		self.assertIn("SHA256", str(caught.exception))
		self.assertEqual(os.listdir(os.path.dirname(destination)), [])

	def test_extract_of_a_missing_member_raises_download_error(self):
		destination = self.destination()
		with self.assertRaises(DownloadError) as caught:
			extract_verified_member(self.make_zip(), ZipMember("other/rclone", sha256_of(MEMBER_DATA)), destination, EXECUTABLE_MODE)
		self.assertIn("other/rclone", str(caught.exception))
		self.assertEqual(os.listdir(os.path.dirname(destination)), [])

	def test_extract_keeps_a_matching_file(self):
		zip_path = self.make_zip()
		member = ZipMember(MEMBER_NAME, sha256_of(MEMBER_DATA))
		destination = self.destination()
		extract_verified_member(zip_path, member, destination, EXECUTABLE_MODE)
		os.utime(destination, ns=(OLD_TIME_NS, OLD_TIME_NS))
		modified = os.stat(destination).st_mtime_ns
		extract_verified_member(zip_path, member, destination, EXECUTABLE_MODE)
		self.assertEqual(os.stat(destination).st_mtime_ns, modified)

	def write_old_destination(self):
		destination = self.destination()
		os.makedirs(os.path.dirname(destination), exist_ok=True)
		with open(destination, "wb") as file:
			file.write(b"old binary")
		return destination

	def test_extract_replaces_a_wrong_file(self):
		destination = self.write_old_destination()
		extract_verified_member(self.make_zip(), ZipMember(MEMBER_NAME, sha256_of(MEMBER_DATA)), destination, EXECUTABLE_MODE)
		with open(destination, "rb") as file:
			self.assertEqual(file.read(), MEMBER_DATA)

	def test_extract_with_a_wrong_hash_deletes_an_old_file(self):
		destination = self.write_old_destination()
		with self.assertRaises(DownloadError):
			extract_verified_member(self.make_zip(), ZipMember(MEMBER_NAME, "0" * 64), destination, EXECUTABLE_MODE)
		self.assertEqual(os.listdir(os.path.dirname(destination)), [])

	def test_extract_fixes_the_mode_of_a_matching_file(self):
		zip_path = self.make_zip()
		member = ZipMember(MEMBER_NAME, sha256_of(MEMBER_DATA))
		destination = self.destination()
		extract_verified_member(zip_path, member, destination, EXECUTABLE_MODE)
		os.chmod(destination, WRONG_MODE)
		extract_verified_member(zip_path, member, destination, EXECUTABLE_MODE)
		self.assertEqual(stat.S_IMODE(os.stat(destination).st_mode), EXECUTABLE_MODE)


if __name__ == "__main__":
	unittest.main()
