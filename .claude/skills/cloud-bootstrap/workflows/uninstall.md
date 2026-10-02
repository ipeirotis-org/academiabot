# Uninstall

To completely remove cloud-bootstrap from a repo:

1. **Remove encrypted credential files:**
   ```bash
   rm -f .cloud-credentials.*.enc
   ```
2. **Remove config:**
   ```bash
   rm -f .cloud-config.json
   ```
3. **Remove the session hooks:**
   - Delete `.claude/hooks/cloud-auth.sh` and `.claude/hooks/cloud-cleanup.sh`
   - Remove the `SessionStart` and `SessionEnd` entries from `.claude/settings.json` (or delete the file if the hooks were the only content)
4. **Clean up `.gitignore`:** Remove the `credentials.json` and `/tmp/credentials.json` lines.
5. **Remove the `## Cloud Credentials` section from AGENTS.md** (if present).
6. **Revoke provider-side credentials:**
   - **GCP:** Delete the service account or its keys
   - **AWS:** Delete the IAM user(s) and group
   - **Azure:** Delete the app registration or its client secrets

   Ask the user for a bootstrap token to perform these provider-side deletions, or instruct them to do it manually via the cloud console.
7. **Commit all changes.**

**Important:** This does not remove the skill files from `.claude/skills/cloud-bootstrap/`. Those can be kept (no secrets) or removed separately.
