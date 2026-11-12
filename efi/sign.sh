#!/bin/sh
# Signs the wn-acpi UEFI driver with the wn Secure Boot key and copies the certificate next to it.
set -eu

cd "$(dirname "$0")"
KEY=keys/wn-sb.key
CERT=keys/wn-sb.crt
CER=keys/wn-sb.cer
IN=wn-acpi/target/aarch64-unknown-uefi/release/wn-acpi.efi
OUT=dist/wn-acpi.efi

mkdir -p dist
rm -f "$OUT"
osslsigncode sign -certs "$CERT" -key "$KEY" -in "$IN" -out "$OUT"
osslsigncode verify -CAfile "$CERT" -in "$OUT"
cp "$CER" dist/wn-sb.cer
