FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /runtime
COPY pyproject.toml uv.lock README.md setup.py ./
COPY src/iac_code/__init__.py ./src/iac_code/__init__.py
RUN uv sync --locked --extra a2a --extra agui --group dev --no-install-project
ENV PATH="/runtime/.venv/bin:${PATH}"
ENV PYTHONPATH="/repo/src:/repo"
WORKDIR /repo
ENTRYPOINT ["python", "-m", "scripts.a2a.e2e.permission_wait.namespace_fixture"]
