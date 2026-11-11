#!/bin/bash
# Builds bin/wn-keygrab and signs it ad-hoc. The Accessibility permission is
# tied to the signed binary identity, so the signature is part of the build.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p bin
swiftc -O -o bin/wn-keygrab main.swift
codesign -s - --force bin/wn-keygrab
echo "built $(pwd)/bin/wn-keygrab"
