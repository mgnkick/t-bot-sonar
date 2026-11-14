#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
site_root=${1:-/var/www/wifi-sonar}

find "$repo_dir/web" -type f -print | while IFS= read -r source; do
    relative=${source#"$repo_dir/web/"}
    install -D -m 0644 "$source" "$site_root/$relative"
done

python3 "$repo_dir/scripts/add-guide-link.py" "$site_root"

printf 'Installed guide pages and sitemap under %s\n' "$site_root"
