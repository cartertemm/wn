//! UEFI boot service driver that installs an SSDT with a battery and an AC adapter.
//!
//! The battery state lives in one page of ACPI NVS memory at a fixed guest physical address, which
//! the Mac host updates while Windows runs. See docs/research/17-uefi-battery.md.

#![no_std]
#![no_main]

use core::ffi::c_void;
use core::ptr;

use uefi::boot::{self, AllocateType, MemoryType, OpenProtocolAttributes, OpenProtocolParams, SearchType};
use uefi::proto::unsafe_protocol;
use uefi::{entry, guid, println, Guid, Status};

const INSTALLED_MARKER: Guid = guid!("7e219b36-f1b6-455f-9dac-839b199706fb");
const SSDT_TEMPLATE: &[u8] = include_bytes!("../ssdt-battery.aml");
const SSDT_LENGTH: usize = SSDT_TEMPLATE.len();
const PLACEHOLDER: [u8; 8] = 0x5151_5151_5151_5151u64.to_le_bytes();
const CHECKSUM_OFFSET: usize = 9;

const FIRST_ADDRESS: u64 = 0x5000_0000;
const ADDRESS_STEP: u64 = 0x10_0000;
const ADDRESS_CANDIDATES: u64 = 16;
const PAGE_COUNT: usize = 1;

const MAGIC: [u8; 4] = *b"WNBT";
const VERSION: u32 = 1;
const FLAG_PRESENT: u32 = 1 << 0;
const FLAG_AC_ONLINE: u32 = 1 << 1;
const INITIAL_PERCENT: u32 = 100;
const FULL_MWH: u32 = 10000;
const DESIGN_MV: u32 = 11400;

const OFFSET_VERSION: usize = 4;
const OFFSET_FLAGS: usize = 8;
const OFFSET_PERCENT: usize = 12;
const OFFSET_RATE: usize = 16;
const OFFSET_REMAINING: usize = 20;
const OFFSET_FULL: usize = 24;
const OFFSET_DESIGN_MV: usize = 28;
const OFFSET_UPDATED: usize = 32;

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

fn allocate_data_page() -> Option<u64> {
	(0..ADDRESS_CANDIDATES)
		.map(|index| FIRST_ADDRESS + index * ADDRESS_STEP)
		.find(|&address| {
			boot::allocate_pages(AllocateType::Address(address), MemoryType::ACPI_NON_VOLATILE, PAGE_COUNT).is_ok()
		})
}

unsafe fn write_u32(base: *mut u8, offset: usize, value: u32) {
	ptr::write_volatile(base.add(offset) as *mut [u8; 4], value.to_le_bytes());
}

unsafe fn write_initial_block(address: u64) {
	let base = address as *mut u8;
	ptr::write_bytes(base, 0, boot::PAGE_SIZE);
	ptr::write_volatile(base as *mut [u8; 4], MAGIC);
	write_u32(base, OFFSET_VERSION, VERSION);
	write_u32(base, OFFSET_FLAGS, FLAG_PRESENT | FLAG_AC_ONLINE);
	write_u32(base, OFFSET_PERCENT, INITIAL_PERCENT);
	write_u32(base, OFFSET_RATE, 0);
	write_u32(base, OFFSET_REMAINING, FULL_MWH);
	write_u32(base, OFFSET_FULL, FULL_MWH);
	write_u32(base, OFFSET_DESIGN_MV, DESIGN_MV);
	ptr::write_volatile(base.add(OFFSET_UPDATED) as *mut [u8; 8], 0u64.to_le_bytes());
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

#[entry]
fn main() -> Status {
	if already_installed() {
		println!("wn-acpi: battery table already installed, skipping");
		return Status::SUCCESS;
	}
	let Some(address) = allocate_data_page() else {
		println!("wn-acpi: no free page for the battery data block");
		return Status::OUT_OF_RESOURCES;
	};
	unsafe { write_initial_block(address) };
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
