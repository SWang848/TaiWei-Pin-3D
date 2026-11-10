#!/usr/bin/env bash
# Extract ZIP and TAR archives beside them, overwriting existing files.
# Skip the top-level results/ directory inside output.tar.
# Delete each archive only after successful extraction.
set -uo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: bash %s DIRECTORY\n' "$0" >&2
    exit 1
fi

root=$(cd -- "$1" && pwd -P) || exit 1

extract_archive() {
    local archive=$1
    case "$archive" in
        *.[zZ][iI][pP]) unzip -o "$archive" -d "${archive%/*}" </dev/null ;;
        */output.tar)
            printf 'Skipping results/ inside: %s\n' "$archive"
            tar -xf "$archive" -C "${archive%/*}" \
                --anchored --exclude='results' --exclude='./results' </dev/null
            ;;
        *) tar -xf "$archive" -C "${archive%/*}" </dev/null ;;
    esac
}

printf 'Searching for ZIP and TAR archives in: %s\n' "$root"
find "$root" -type f \( -iname '*.zip' -o -iname '*.tar' \
    -o -iname '*.tar.gz' -o -iname '*.tgz' \) -print0 | {
    status=0
    found=0
    extracted=0
    while IFS= read -r -d '' archive; do
        found=$((found + 1))
        printf 'Extracting: %s\n' "$archive"
        if extract_archive "$archive"; then
            if rm -- "$archive"; then
                extracted=$((extracted + 1))
                printf 'Deleted: %s\n' "$archive"
            else
                status=1
            fi
        else
            printf 'Extraction failed; keeping: %s\n' "$archive" >&2
            status=1
        fi
    done
    if [[ $found -eq 0 ]]; then
        printf 'No .zip, .tar, .tar.gz, or .tgz files found.\n'
    else
        printf 'Finished: %d of %d archive(s) extracted and deleted.\n' "$extracted" "$found"
    fi
    exit "$status"
}
