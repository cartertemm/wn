#!/bin/sh
# Signs the wn-acpi UEFI driver with the wn Secure Boot key.
set -eu

cd "$(dirname "$0")"
KEY=keys/wn-sb.key
CERT=keys/wn-sb.crt
IN=wn-acpi/target/aarch64-unknown-uefi/release/wn-acpi.efi
OUT=wn-acpi/wn-acpi-signed.efi

rm -f "$OUT"
osslsigncode sign -certs "$CERT" -key "$KEY" -in "$IN" -out "$OUT"
osslsigncode verify -CAfile "$CERT" -in "$OUT"
