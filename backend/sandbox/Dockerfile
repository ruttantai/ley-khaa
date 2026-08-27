# backend/sandbox/Dockerfile
# The only place synthesized code is allowed to run.
#
# Versions are pinned, not floated: a bundle claims that generator/ re-runs to
# the same deliverable, and a library that silently changes behaviour under it
# turns that claim into a lie.
#
# Keep this library set identical to the backend's own (backend/pyproject.toml).
# The subprocess fallback runs the backend interpreter, so anything installed
# here and missing there makes the two sandboxes disagree.
FROM python:3.12-slim
RUN pip install --no-cache-dir openpyxl==3.1.5 python-docx==1.2.0
WORKDIR /work
