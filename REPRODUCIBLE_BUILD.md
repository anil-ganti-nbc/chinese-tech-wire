# Reproducible builds

Use Python 3.12. Verify `requirements.lock` by compiling `requirements.txt` with uv 0.11.32 and comparing output, then install with `python -m pip install --require-hashes -r requirements.lock`. Build the pinned-base image with `docker build --build-arg GIT_REVISION=FULL_40_CHAR_SHA -t chinese-tech-wire:FULL_40_CHAR_SHA .`. CI records the source archive, SBOM, lock digest, and Git SHA. Do not publish or promote.
