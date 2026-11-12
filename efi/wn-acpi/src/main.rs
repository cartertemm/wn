//! UEFI boot service driver that installs an SSDT with a battery and an AC adapter.
//!
//! The battery state lives in a QEMU NVDIMM backed by a file on the Mac host, which the host
//! updates while Windows runs. At ReadyToBoot the driver finds the persistent memory range in the
//! NFIT, patches its base into the SSDT OperationRegion, and installs the table. It never writes to
//! the range. If the event cannot be registered, it does the same work at entry. See
//! docs/research/17-uefi-battery.md and docs/research/18-nvdimm-channel.md.

#![no_std]
#![no_main]

use core::ffi::c_void;
use core::ptr::{self, NonNull};

use uefi::boot::{self, EventType, OpenProtocolAttributes, OpenProtocolParams, SearchType, Tpl};
use uefi::proto::unsafe_protocol;
use uefi::{entry, guid, println, Event, Guid, Status};

const INSTALLED_MARKER: Guid = guid!("7e219b36-f1b6-455f-9dac-839b199706fb");
const READY_TO_BOOT_GROUP: Guid = guid!("7ce88fb3-4bd7-4679-87a8-a8d8dee50d2b");
const ACPI2_TABLE_GUID: Guid = guid!("8868e871-e4f1-11d3-bc22-0080c73c8881");
const PERSISTENT_MEMORY_GUID: Guid = guid!("66f0d379-b4f3-4074-ac43-0d3318b78cdb");

const SSDT_TEMPLATE: &[u8] = include_bytes!("../ssdt-battery.aml");
const SSDT_LENGTH: usize = SSDT_TEMPLATE.len();
const PLACEHOLDER: [u8; 8] = 0x5151_5151_5151_5151u64.to_le_bytes();
const CHECKSUM_OFFSET: usize = 9;
const REGION_LENGTH: u64 = 64;

const RSDP_XSDT_OFFSET: u64 = 24;
const TABLE_LENGTH_OFFSET: u64 = 4;
const TABLE_HEADER_LENGTH: u64 = 36;
const XSDT_ENTRY_LENGTH: u64 = 8;
const NFIT_SIGNATURE: [u8; 4] = *b"NFIT";
const NFIT_FIRST_STRUCTURE: u64 = 40;
const NFIT_STRUCTURE_HEADER_LENGTH: u64 = 4;
const NFIT_SPA_TYPE: u16 = 0;
const SPA_LENGTH: u64 = 56;
const SPA_RANGE_GUID_OFFSET: u64 = 16;
const SPA_BASE_OFFSET: u64 = 32;
const SPA_LENGTH_OFFSET: u64 = 40;

type InstallAcpiTableFn = unsafe extern "efiapi" fn(
	this: *const AcpiTableProtocol,
	table: *const c_void,
	size: usize,
	key: *mut usize,
) -> Status;

#[repr(C)]
struct AcpiTableProtocol {
	install_acpi_table: InstallAcpiTableFn,
	uninstall_acpi_table: unsafe extern "efiapi" fn(this: *const AcpiTableProtocol, key: usize) -> Status,
}

impl AcpiTableProtocol {
	const GUID: Guid = guid!("ffe06bdd-6107-46a6-7bb2-5a9c7ec5275c");
}

#[repr(transparent)]
#[unsafe_protocol(AcpiTableProtocol::GUID)]
struct AcpiTable(AcpiTableProtocol);

unsafe fn read<T: Copy>(address: u64) -> T {
	ptr::read_unaligned(address as *const T)
}

fn acpi2_rsdp() -> Option<u64> {
	uefi::system::with_config_table(|tables| {
		tables.iter().find(|table| table.guid == ACPI2_TABLE_GUID).map(|table| table.address as u64)
	})
}

unsafe fn find_table(signature: [u8; 4]) -> Option<u64> {
	let rsdp = acpi2_rsdp()?;
	let xsdt: u64 = read(rsdp + RSDP_XSDT_OFFSET);
	if xsdt == 0 {
		return None;
	}
	let xsdt_length = read::<u32>(xsdt + TABLE_LENGTH_OFFSET) as u64;
	let count = xsdt_length.saturating_sub(TABLE_HEADER_LENGTH) / XSDT_ENTRY_LENGTH;
	(0..count)
		.map(|index| read::<u64>(xsdt + TABLE_HEADER_LENGTH + index * XSDT_ENTRY_LENGTH))
		.find(|&table| table != 0 && read::<[u8; 4]>(table) == signature)
}

unsafe fn find_battery_address() -> Option<u64> {
	let nfit = find_table(NFIT_SIGNATURE)?;
	let nfit_length = read::<u32>(nfit + TABLE_LENGTH_OFFSET) as u64;
	let mut offset = NFIT_FIRST_STRUCTURE;
	while offset + NFIT_STRUCTURE_HEADER_LENGTH <= nfit_length {
		let kind: u16 = read(nfit + offset);
		let length = read::<u16>(nfit + offset + 2) as u64;
		if length == 0 || offset + length > nfit_length {
			return None;
		}
		if kind == NFIT_SPA_TYPE && length >= SPA_LENGTH {
			let structure = nfit + offset;
			let range_type = Guid::from_bytes(read(structure + SPA_RANGE_GUID_OFFSET));
			let size: u64 = read(structure + SPA_LENGTH_OFFSET);
			if range_type == PERSISTENT_MEMORY_GUID && size >= REGION_LENGTH {
				return Some(read(structure + SPA_BASE_OFFSET));
			}
		}
		offset += length;
	}
	None
}

fn patched_table(address: u64) -> Option<[u8; SSDT_LENGTH]> {
	let mut table = [0u8; SSDT_LENGTH];
	table.copy_from_slice(SSDT_TEMPLATE);
	let offset = table.windows(PLACEHOLDER.len()).position(|window| window == PLACEHOLDER)?;
	table[offset..offset + PLACEHOLDER.len()].copy_from_slice(&address.to_le_bytes());
	table[CHECKSUM_OFFSET] = 0;
	let sum = table.iter().fold(0u8, |total, byte| total.wrapping_add(*byte));
	table[CHECKSUM_OFFSET] = sum.wrapping_neg();
	Some(table)
}

fn install_table(table: &[u8]) -> Result<(), Status> {
	let handle = boot::get_handle_for_protocol::<AcpiTable>().map_err(|error| error.status())?;
	let parameters = OpenProtocolParams { handle, agent: boot::image_handle(), controller: None };
	let protocol = unsafe { boot::open_protocol::<AcpiTable>(parameters, OpenProtocolAttributes::GetProtocol) }
		.map_err(|error| error.status())?;
	let mut key = 0usize;
	let status = unsafe {
		(protocol.0.install_acpi_table)(&protocol.0, table.as_ptr() as *const c_void, table.len(), &mut key)
	};
	if status.is_success() {
		Ok(())
	} else {
		Err(status)
	}
}

fn already_installed() -> bool {
	boot::locate_handle_buffer(SearchType::ByProtocol(&INSTALLED_MARKER)).is_ok_and(|handles| !handles.is_empty())
}

fn mark_installed() {
	if unsafe { boot::install_protocol_interface(None, &INSTALLED_MARKER, ptr::null()) }.is_err() {
		println!("wn-acpi: cannot install the marker protocol, a second load would add a second table");
	}
}

fn install_battery_table() -> Status {
	if already_installed() {
		println!("wn-acpi: battery table already installed, skipping");
		return Status::SUCCESS;
	}
	let Some(address) = (unsafe { find_battery_address() }) else {
		println!("wn-acpi: no NVDIMM range found, battery table not installed");
		return Status::NOT_FOUND;
	};
	let Some(table) = patched_table(address) else {
		println!("wn-acpi: address placeholder not found in the SSDT");
		return Status::LOAD_ERROR;
	};
	match install_table(&table) {
		Ok(()) => {
			mark_installed();
			println!("wn-acpi: battery table installed, data at {:#x}", address);
			Status::SUCCESS
		}
		Err(status) => {
			println!("wn-acpi: InstallAcpiTable failed: {:?}", status);
			status
		}
	}
}

unsafe extern "efiapi" fn on_ready_to_boot(event: Event, _context: Option<NonNull<c_void>>) {
	let _ = install_battery_table();
	let _ = boot::close_event(event);
}

fn register_ready_to_boot() -> bool {
	let group = NonNull::from(&READY_TO_BOOT_GROUP);
	unsafe { boot::create_event_ex(EventType::NOTIFY_SIGNAL, Tpl::CALLBACK, Some(on_ready_to_boot), None, Some(group)) }
		.is_ok()
}

#[entry]
fn main() -> Status {
	if already_installed() {
		println!("wn-acpi: battery table already installed, skipping");
		return Status::SUCCESS;
	}
	if register_ready_to_boot() {
		println!("wn-acpi: waiting for ReadyToBoot");
		return Status::SUCCESS;
	}
	println!("wn-acpi: cannot register for ReadyToBoot, installing now");
	install_battery_table()
}
