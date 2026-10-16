#!/bin/bash
# SessionEnd hook: remove the per-session service account key written by cloud-auth.sh.
f="${GOOGLE_APPLICATION_CREDENTIALS:-}"
case "$f" in
  "${TMPDIR:-/tmp}"/gcp-sa-*.json) rm -f "$f" ;;
esac
exit 0
