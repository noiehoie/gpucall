# SBOM

Generate the final SBOM during release:

```bash
python -m pip freeze > sbom-python.txt
npm --prefix sdk/typescript ls --all --json > sbom-typescript.json
docker image inspect gpucall-gpucall > sbom-container-image.json
```

Current direct runtime dependencies are declared in `pyproject.toml`:

- boto3
- fastapi
- httpx
- pydantic
- pyyaml
- uvicorn

TypeScript direct dependency:

- typescript
