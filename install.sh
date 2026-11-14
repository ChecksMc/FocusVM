#!/bin/sh
set -eu
source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec "${FOCUSVM_PYTHON:-python3}" "$source_dir/scripts/install.py" install "$@"
