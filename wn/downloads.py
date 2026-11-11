"""Fetch and verify the files a Windows 11 ARM64 UTM VM install needs."""

import dataclasses
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from wn.errors import WnError

DEFAULT_DOWNLOADS_DIR = os.path.join("~", "Library", "Application Support", "wn", "downloads")
CACHE_FILE_NAME = "verified.json"
PART_SUFFIX = ".part"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
REQUEST_TIMEOUT_SECONDS = 60
CHUNK_SIZE = 1024 * 1024
BYTES_PER_MB = 1000 ** 2
BYTES_PER_GB = 1000 ** 3
PROGRESS_PERCENT_STEP = 5
PROGRESS_INTERVAL_SECONDS = 10
FULL_PERCENT = 100
MAXIMUM_REDIRECTS = 10
REDIRECT_CODES = (301, 302, 303, 307, 308)
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

WINDOWS_LABEL = "Windows ISO"
VIRTIO_LABEL = "VirtIO drivers ISO"
GUEST_TOOLS_LABEL = "UTM guest tools ISO"
NVDA_LABEL = "NVDA installer"
WINDOWS_ROLE = "windows_iso"
VIRTIO_ROLE = "virtio_iso"
GUEST_TOOLS_ROLE = "guest_tools_iso"
NVDA_ROLE = "nvda_installer"

# The Microsoft flow follows Fido (https://github.com/pbatard/Fido), which documents these endpoints and ids.
WINDOWS_PAGE_URL = "https://www.microsoft.com/en-us/software-download/windows11arm64"
WINDOWS_REFERER = "https://www.microsoft.com/software-download/windows11"
MICROSOFT_TAGS_URL = "https://vlscppe.microsoft.com/tags"
MICROSOFT_MDT_URL = "https://ov-df.microsoft.com/mdt.js"
MICROSOFT_FINGERPRINT_URL = "https://ov-df.microsoft.com/"
MICROSOFT_SKU_URL = "https://www.microsoft.com/software-download-connector/api/getskuinformationbyproductedition"
MICROSOFT_LINKS_URL = "https://www.microsoft.com/software-download-connector/api/GetProductDownloadLinksBySku"
MICROSOFT_ORG_ID = "y6jn8c31"
MICROSOFT_PROFILE_ID = "606624d44113"
MICROSOFT_INSTANCE_ID = "560dc9f3-1aa5-4a2f-b63c-9e18f8d0e175"
MICROSOFT_PAGE_ID = "si"
MICROSOFT_LOCALE = "en-US"
MICROSOFT_UNDEFINED = "undefined"
ENGLISH_UNITED_STATES_LANGUAGE = "English"
ENGLISH_HASH_ROW_PATTERN = re.compile(r"^english (64-bit|arm64)$")
ARM64_DOWNLOAD_TYPE = 2
MICROSOFT_BAN_ERROR_TYPE = 9
MICROSOFT_BAN_MARKERS = ("715-123130", "unable to complete your request", "sentinel")
MICROSOFT_BAN_MESSAGE = (
	"Microsoft refused the Windows download request ({}). "
	"Microsoft blocks an IP address for a while after too many ISO download requests or when it is in a sanctioned region "
	"(message code 715-123130). Try again later or from another network."
)

GITHUB_API_HEADERS = {"Accept": "application/vnd.github+json"}
GITHUB_RELEASE_BY_TAG_URL = "https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
GITHUB_RELEASE_DOWNLOAD_PATTERN = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)$")
GITHUB_DIGEST_PREFIX = "sha256:"
VIRTIO_RELEASE_API_URL = "https://api.github.com/repos/qemus/virtiso-arm/releases/latest"
VIRTIO_VERSIONED_ISO_PATTERN = re.compile(r"^virtio-win-[0-9.]+\.iso$")
ISO_SUFFIX = ".iso"
GUEST_TOOLS_LATEST_URL = "https://getutm.app/downloads/utm-guest-tools-latest.iso"
NVDA_DOWNLOAD_PAGE_URL = "https://www.nvaccess.org/download/"
NVDA_VERSION_PATTERN = re.compile(r"releases/([0-9]+(?:\.[0-9]+)+)/nvda_\1\.exe")
NVDA_INSTALLER_URL = "https://download.nvaccess.org/releases/{version}/nvda_{version}.exe"
NVDA_INSTALLER_NAME = "nvda_{version}.exe"
NVDA_GITHUB_OWNER = "nvaccess"
NVDA_GITHUB_REPO = "nvda"
NVDA_RELEASE_TAG = "release-{version}"
NVDA_RELEASE_NOTES_HASH_PATTERN = re.compile(r"SHA256 sum:\s*`?([0-9a-fA-F]{64})")


@dataclasses.dataclass(frozen=True)
class Downloads:
	windows_iso: str
	virtio_iso: str
	guest_tools_iso: str
	nvda_installer: Optional[str]


class DownloadError(WnError):
	"""Raised when a file cannot be looked up, downloaded, or verified."""


@dataclasses.dataclass(frozen=True)
class _Source:
	url: str
	file_name: str
	sha256: str


def parse_product_edition_id(page_html):
	"""Return the product edition id from the ARM64 download page's edition picker."""
	options = re.findall(r'<option value="([0-9]+)"[^>]*>([^<]*)', page_html)
	for edition_id, text in options:
		if "arm64" in text.lower():
			return edition_id
	if options:
		return options[0][0]
	raise DownloadError("cannot find the Windows 11 ARM64 edition on {}".format(WINDOWS_PAGE_URL))


def parse_windows_english_hash(page_html):
	"""Return the lowercase SHA256 that the download page publishes for English (United States)."""
	for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.DOTALL):
		cells = [re.sub(r"<[^>]+>", "", cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
		if len(cells) != 2:
			continue
		language = " ".join(cells[0].split()).lower()
		sha256 = cells[1].strip()
		if ENGLISH_HASH_ROW_PATTERN.match(language) and SHA256_PATTERN.match(sha256):
			return sha256.lower()
	raise DownloadError("cannot find the English 64-bit SHA256 on {}".format(WINDOWS_PAGE_URL))


def _microsoft_errors(response):
	return response.get("Errors") or (response.get("ValidationContainer") or {}).get("Errors") or []


def _is_microsoft_ban(text):
	return any(marker in text.lower() for marker in MICROSOFT_BAN_MARKERS)


def _raise_microsoft_errors(response, ban_error_types=()):
	errors = _microsoft_errors(response)
	if not errors:
		return
	first = errors[0]
	description = "{}: {}".format(first.get("Key", "error"), first.get("Value", ""))
	if first.get("Type") in ban_error_types or _is_microsoft_ban(description):
		raise DownloadError(MICROSOFT_BAN_MESSAGE.format(description))
	raise DownloadError("Microsoft answered with an error: {}".format(description))


def pick_english_sku(sku_response):
	"""Return the SKU id for English (United States) from a getskuinformationbyproductedition answer."""
	_raise_microsoft_errors(sku_response)
	for sku in sku_response.get("Skus") or []:
		if sku.get("Language") == ENGLISH_UNITED_STATES_LANGUAGE:
			return sku["Id"]
	raise DownloadError("Microsoft did not offer an English (United States) Windows 11 ARM64 ISO")


def pick_arm64_link(links_response):
	"""Return the ARM64 ISO link from a GetProductDownloadLinksBySku answer."""
	_raise_microsoft_errors(links_response, ban_error_types=(MICROSOFT_BAN_ERROR_TYPE,))
	for option in links_response.get("ProductDownloadOptions") or []:
		if option.get("DownloadType") == ARM64_DOWNLOAD_TYPE and option.get("Uri"):
			return option["Uri"]
	raise DownloadError("Microsoft did not offer an ARM64 download link")


def parse_github_digest(asset):
	"""Return the lowercase SHA256 from a GitHub release asset's ``digest`` field."""
	digest = asset.get("digest") or ""
	sha256 = digest[len(GITHUB_DIGEST_PREFIX):]
	if not digest.startswith(GITHUB_DIGEST_PREFIX) or not SHA256_PATTERN.match(sha256):
		raise DownloadError("GitHub has no SHA256 digest for {}".format(asset.get("name")))
	return sha256.lower()


def parse_nvda_version(page_html):
	"""Return the stable NVDA version linked from the NV Access download page."""
	match = NVDA_VERSION_PATTERN.search(page_html)
	if match is None:
		raise DownloadError("cannot find the NVDA version on {}".format(NVDA_DOWNLOAD_PAGE_URL))
	return match.group(1)


def _safe_file_name(name):
	file_name = urllib.parse.unquote(name)
	if file_name in ("", ".", "..") or "/" in file_name or "\\" in file_name:
		raise DownloadError("refusing unsafe file name {!r}".format(name))
	return file_name


def _format_size(byte_count, unit_bytes):
	return "{:.1f}".format(byte_count / unit_bytes)


class Progress:
	"""Report download progress at most every few percent or every few seconds."""

	def __init__(self, label, total_bytes, report, clock=time.monotonic):
		self._label = label
		self._total_bytes = total_bytes
		self._report = report
		self._clock = clock
		self._last_percent = 0
		self._last_time = clock()

	def update(self, done_bytes):
		now = self._clock()
		interval_passed = now - self._last_time >= PROGRESS_INTERVAL_SECONDS
		if not self._total_bytes:
			if interval_passed:
				self._report("{}: {} MB".format(self._label, _format_size(done_bytes, BYTES_PER_MB)))
				self._last_time = now
			return
		percent = done_bytes * FULL_PERCENT // self._total_bytes
		finished = done_bytes >= self._total_bytes
		if not (interval_passed or finished or percent >= self._last_percent + PROGRESS_PERCENT_STEP):
			return
		unit_name, unit_bytes = ("GB", BYTES_PER_GB) if self._total_bytes >= BYTES_PER_GB else ("MB", BYTES_PER_MB)
		self._report("{}: {}% ({} of {} {})".format(self._label, percent, _format_size(done_bytes, unit_bytes), _format_size(self._total_bytes, unit_bytes), unit_name))
		self._last_percent = percent
		self._last_time = now


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, request, response, code, message, headers, new_url):
		return None


def _default_opener():
	return urllib.request.build_opener(_NoRedirectHandler())


class _Http:
	"""Send requests with a browser User-Agent and follow redirects by hand so they stay visible."""

	def __init__(self, opener):
		self._open = opener.open if hasattr(opener, "open") else opener

	def _send(self, url, headers):
		request = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": USER_AGENT}))
		try:
			return self._open(request, timeout=REQUEST_TIMEOUT_SECONDS)
		except urllib.error.HTTPError as error:
			if error.code in REDIRECT_CODES:
				return error
			error.close()
			raise DownloadError("{} answered HTTP {} {}".format(url, error.code, error.reason)) from None
		except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
			raise DownloadError("cannot reach {}: {}".format(url, getattr(error, "reason", error))) from None

	def first_redirect(self, url):
		response = self._send(url, None)
		with response:
			if response.getcode() in REDIRECT_CODES:
				return urllib.parse.urljoin(url, response.headers["Location"])
			return response.geturl()

	def open(self, url, headers=None):
		for _ in range(MAXIMUM_REDIRECTS):
			response = self._send(url, headers)
			if response.getcode() not in REDIRECT_CODES:
				return response
			response.close()
			url = urllib.parse.urljoin(url, response.headers["Location"])
		raise DownloadError("too many redirects for {}".format(url))

	def read(self, url, headers=None):
		with self.open(url, headers) as response:
			try:
				return response.read()
			except (OSError, http.client.HTTPException) as error:
				raise DownloadError("cannot read {}: {}".format(url, error)) from None

	def read_json(self, url, headers=None):
		body = self.read(url, headers)
		try:
			return json.loads(body.decode("utf-8"))
		except ValueError:
			raise DownloadError("{} did not answer with JSON".format(url)) from None


def _with_query(url, parameters):
	return url + "?" + urllib.parse.urlencode(parameters)


def _read_microsoft_json(http, url, headers=None):
	body = http.read(url, headers).decode("utf-8", "replace")
	try:
		return json.loads(body)
	except ValueError:
		if _is_microsoft_ban(body):
			raise DownloadError(MICROSOFT_BAN_MESSAGE.format("unable to complete your request")) from None
		raise DownloadError("Microsoft answered {} with something other than JSON".format(url)) from None


def _register_microsoft_session(http, session_id):
	http.read(_with_query(MICROSOFT_TAGS_URL, {"org_id": MICROSOFT_ORG_ID, "session_id": session_id}))
	script = http.read(_with_query(MICROSOFT_MDT_URL, {"instanceId": MICROSOFT_INSTANCE_ID, "PageId": MICROSOFT_PAGE_ID, "session_id": session_id})).decode("utf-8", "replace")
	w_match = re.search(r"[?&]w=([A-F0-9]+)", script)
	rticks_match = re.search(r'rticks="\+?([0-9]+)', script)
	if w_match is None or rticks_match is None:
		raise DownloadError("cannot read Microsoft's download session script from {}".format(MICROSOFT_MDT_URL))
	http.read(_with_query(MICROSOFT_FINGERPRINT_URL, {
		"session_id": session_id,
		"CustomerId": MICROSOFT_INSTANCE_ID,
		"PageId": MICROSOFT_PAGE_ID,
		"w": w_match.group(1),
		"mdt": int(time.time() * 1000),
		"rticks": rticks_match.group(1),
	}))


def _windows_page(http):
	"""Return the ARM64 product edition id and the published English SHA256."""
	page = http.read(WINDOWS_PAGE_URL).decode("utf-8", "replace")
	return parse_product_edition_id(page), parse_windows_english_hash(page)


def _windows_source(http, edition_id, sha256):
	session_id = str(uuid.uuid4())
	_register_microsoft_session(http, session_id)
	sku_id = pick_english_sku(_read_microsoft_json(http, _with_query(MICROSOFT_SKU_URL, {
		"profile": MICROSOFT_PROFILE_ID,
		"productEditionId": edition_id,
		"SKU": MICROSOFT_UNDEFINED,
		"friendlyFileName": MICROSOFT_UNDEFINED,
		"Locale": MICROSOFT_LOCALE,
		"sessionID": session_id,
	})))
	link = pick_arm64_link(_read_microsoft_json(http, _with_query(MICROSOFT_LINKS_URL, {
		"profile": MICROSOFT_PROFILE_ID,
		"productEditionId": MICROSOFT_UNDEFINED,
		"SKU": sku_id,
		"friendlyFileName": MICROSOFT_UNDEFINED,
		"Locale": MICROSOFT_LOCALE,
		"sessionID": session_id,
	}), {"Referer": WINDOWS_REFERER}))
	file_name = _safe_file_name(urllib.parse.urlsplit(link).path.split("/")[-1])
	return _Source(link, file_name, sha256)


def _release_asset(release, name):
	for asset in release.get("assets") or []:
		if asset.get("name") == name:
			return asset
	raise DownloadError("release {} has no asset named {}".format(release.get("tag_name"), name))


def _virtio_source(http):
	release = http.read_json(VIRTIO_RELEASE_API_URL, GITHUB_API_HEADERS)
	isos = [asset for asset in release.get("assets") or [] if asset.get("name", "").endswith(ISO_SUFFIX)]
	versioned = [asset for asset in isos if VIRTIO_VERSIONED_ISO_PATTERN.match(asset["name"])]
	if not isos:
		raise DownloadError("virtio-win release {} has no ISO".format(release.get("tag_name")))
	asset = (versioned or isos)[0]
	return _Source(asset["browser_download_url"], _safe_file_name(asset["name"]), parse_github_digest(asset))


def _guest_tools_source(http):
	asset_url = http.first_redirect(GUEST_TOOLS_LATEST_URL)
	match = GITHUB_RELEASE_DOWNLOAD_PATTERN.match(asset_url)
	if match is None:
		raise DownloadError("{} no longer points to a GitHub release ({})".format(GUEST_TOOLS_LATEST_URL, asset_url))
	owner, repo, tag, name = match.groups()
	release = http.read_json(GITHUB_RELEASE_BY_TAG_URL.format(owner=owner, repo=repo, tag=tag), GITHUB_API_HEADERS)
	asset = _release_asset(release, urllib.parse.unquote(name))
	return _Source(asset_url, _safe_file_name(asset["name"]), parse_github_digest(asset))


def _nvda_source(http):
	"""Expected hash: the "SHA256 sum" NV Access writes in the GitHub release notes, cross-checked with GitHub's asset digest."""
	version = parse_nvda_version(http.read(NVDA_DOWNLOAD_PAGE_URL).decode("utf-8", "replace"))
	file_name = NVDA_INSTALLER_NAME.format(version=version)
	release_url = GITHUB_RELEASE_BY_TAG_URL.format(owner=NVDA_GITHUB_OWNER, repo=NVDA_GITHUB_REPO, tag=NVDA_RELEASE_TAG.format(version=version))
	release = http.read_json(release_url, GITHUB_API_HEADERS)
	notes_match = NVDA_RELEASE_NOTES_HASH_PATTERN.search(release.get("body") or "")
	asset_sha256 = parse_github_digest(_release_asset(release, file_name))
	if notes_match and notes_match.group(1).lower() != asset_sha256:
		raise DownloadError("NVDA {} release notes give SHA256 {} but GitHub's asset digest is {}".format(version, notes_match.group(1).lower(), asset_sha256))
	return _Source(NVDA_INSTALLER_URL.format(version=version), file_name, asset_sha256)


def _sha256_of_file(path):
	digest = hashlib.sha256()
	with open(path, "rb") as file:
		for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _remove_if_present(path):
	try:
		os.remove(path)
	except FileNotFoundError:
		pass


class _Fetcher:
	def __init__(self, directory, http, report):
		self._directory = directory
		self._http = http
		self._report = report
		self._cache_path = os.path.join(directory, CACHE_FILE_NAME)
		self._cache = self._load_cache()

	def _load_cache(self):
		try:
			with open(self._cache_path, encoding="utf-8") as file:
				cache = json.load(file)
		except (OSError, ValueError):
			return {}
		return cache if isinstance(cache, dict) else {}

	def _save_cache(self):
		temporary_path = self._cache_path + PART_SUFFIX
		with open(temporary_path, "w", encoding="utf-8") as file:
			json.dump(self._cache, file, indent="\t", sort_keys=True)
		os.replace(temporary_path, self._cache_path)

	def _record_verified(self, role, path, sha256):
		status = os.stat(path)
		self._cache[os.path.basename(path)] = {"role": role, "path": path, "size": status.st_size, "mtime_ns": status.st_mtime_ns, "sha256": sha256}
		self._save_cache()

	def _cached_sha256(self, path):
		entry = self._cache.get(os.path.basename(path))
		if not isinstance(entry, dict):
			return None
		try:
			status = os.stat(path)
		except OSError:
			return None
		if entry.get("size") == status.st_size and entry.get("mtime_ns") == status.st_mtime_ns:
			return entry.get("sha256")
		return None

	def cached_verified_path(self, role):
		paths = [os.path.join(self._directory, name) for name, entry in self._cache.items() if isinstance(entry, dict) and entry.get("role") == role]
		trusted = [path for path in paths if self._cached_sha256(path)]
		return max(trusted, key=os.path.getmtime) if trusted else None

	def _is_verified(self, role, label, path, sha256):
		if self._cached_sha256(path) == sha256:
			return True
		self._report("{}: checking the SHA256 of the existing {}".format(label, os.path.basename(path)))
		if _sha256_of_file(path) != sha256:
			return False
		self._record_verified(role, path, sha256)
		return True

	def ensure(self, role, label, source):
		path = os.path.join(self._directory, source.file_name)
		if os.path.exists(path):
			if self._is_verified(role, label, path, source.sha256):
				self._report("{}: already downloaded and verified".format(label))
				return path
			self._report("{}: the existing {} does not match the published SHA256, downloading it again".format(label, source.file_name))
		self._download(label, source, path)
		self._record_verified(role, path, source.sha256)
		self._report("{}: downloaded and verified {}".format(label, source.file_name))
		return path

	def _download(self, label, source, path):
		part_path = path + PART_SUFFIX
		digest = hashlib.sha256()
		try:
			with self._http.open(source.url) as response, open(part_path, "wb") as part_file:
				length = response.headers.get("Content-Length")
				progress = Progress(label, int(length) if length and length.isdigit() else None, self._report)
				done_bytes = 0
				for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
					part_file.write(chunk)
					digest.update(chunk)
					done_bytes += len(chunk)
					progress.update(done_bytes)
		except (OSError, http.client.HTTPException) as error:
			_remove_if_present(part_path)
			raise DownloadError("{}: download of {} failed: {}".format(label, source.file_name, error)) from None
		except BaseException:
			_remove_if_present(part_path)
			raise
		actual = digest.hexdigest()
		if actual != source.sha256:
			_remove_if_present(part_path)
			raise DownloadError("{}: SHA256 of the downloaded {} is {} but the published SHA256 is {}. The file was deleted.".format(label, source.file_name, actual, source.sha256))
		os.replace(part_path, path)

	def windows_iso(self):
		try:
			edition_id, sha256 = _windows_page(self._http)
			cached_path = self.cached_verified_path(WINDOWS_ROLE)
			if cached_path is not None and self._cached_sha256(cached_path) == sha256:
				self._report("{}: already downloaded and verified".format(WINDOWS_LABEL))
				return cached_path
			source = _windows_source(self._http, edition_id, sha256)
		except DownloadError as error:
			cached_path = self.cached_verified_path(WINDOWS_ROLE)
			if cached_path is None:
				raise
			self._report("{}: could not check Microsoft ({}); using the previously verified {}".format(WINDOWS_LABEL, error, os.path.basename(cached_path)))
			return cached_path
		return self.ensure(WINDOWS_ROLE, WINDOWS_LABEL, source)


def fetch_all(downloads_dir, include_nvda=True, report=print, opener=None):
	"""Download and verify everything a Windows 11 ARM64 UTM install needs into ``downloads_dir``."""
	directory = os.path.abspath(os.path.expanduser(downloads_dir))
	os.makedirs(directory, exist_ok=True)
	http = _Http(opener or _default_opener())
	fetcher = _Fetcher(directory, http, report)
	windows_iso = fetcher.windows_iso()
	virtio_iso = fetcher.ensure(VIRTIO_ROLE, VIRTIO_LABEL, _virtio_source(http))
	guest_tools_iso = fetcher.ensure(GUEST_TOOLS_ROLE, GUEST_TOOLS_LABEL, _guest_tools_source(http))
	nvda_installer = fetcher.ensure(NVDA_ROLE, NVDA_LABEL, _nvda_source(http)) if include_nvda else None
	return Downloads(windows_iso, virtio_iso, guest_tools_iso, nvda_installer)
