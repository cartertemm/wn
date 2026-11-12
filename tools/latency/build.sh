#!/bin/sh
set -e
cd "$(dirname "$0")"
mkdir -p bin
swiftc -O record_beep.swift -o bin/record_beep
