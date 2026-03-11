#!/usr/bin/env bash
set -euo pipefail
cd /home/site/wwwroot
# keep container alive with a tiny static server on 8000
exec python -m http.server 8000
