#!/bin/sh
# Signs the wn-acpi UEFI driver with the wn Secure Boot key and copies the certificate next to it.
# The key lives outside the repo in $WN_SB_KEYS (default ~/.config/wn/keys) and must be mode 0600.
set -eu

cd "$(dirname "$0")"
KEYS=${WN_SB_KEYS:-$HOME/.config/wn/keys}
KEY=$KEYS/wn-sb.key
CERT=$KEYS/wn-sb.crt
CER=$KEYS/wn-sb.cer
IN=wn-acpi/target/aarch64-unknown-uefi/release/wn-acpi.efi
OUT=dist/wn-acpi.efi

if [ ! -f "$KEY" ]; then
	echo "sign.sh: no signing key at $KEY" >&2
	exit 1
fi
if [ -n "$(find "$KEY" -perm +077)" ]; then
	echo "sign.sh: $KEY is readable by other users, run: chmod 600 $KEY" >&2
	exit 1
fi

mkdir -p dist
rm -f "$OUT"
osslsigncode sign -certs "$CERT" -key "$KEY" -in "$IN" -out "$OUT"
osslsigncode verify -CAfile "$CERT" -in "$OUT"
cp "$CER" dist/wn-sb.cer
