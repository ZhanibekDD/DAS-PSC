# Development contract

- Work on the feature branch and PR; do not merge or deploy without explicit authorization.
- Never connect, restart, migrate, or alter existing DAS / procurement / production services.
- Never commit real archives, customer documents, photos, NAS paths, databases, credentials or private test manifests. Test only synthetic data in CI. Do not print real filenames to CI logs.
- Filesystem operations are read-only for sources. Never auto-extract ZIP entries into source folders. Persist only registry metadata after explicit confirmation.
- Category presence is not document completeness or construction progress. Do not add invented percentages or sample business records to a real database.
- Preserve source template terminology; clarify typos in documentation, not by silently changing requirements.
- Keep import confirmation atomic and idempotent. Retain versions and cross-project isolation. Never overwrite user-confirmed classifications on reimport.
- Any new server deployment must have authentication, HTTPS, per-user authorization before broader use, read-only source mounts and a tested backup/restore plan.
- Run pytest -q, installed-wheel smoke tests and Docker CI. Report exactly what ran; a prepared Dockerfile is not a verified running deployment.
