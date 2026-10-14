# Releasing

## Cutting a release

1. Bump the version in both `python/pyproject.toml` and `python/src/agentplay/__init__.py`.
2. Merge to `main`, then tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Pushing the tag starts `release.yml` (it triggers on tags matching `v*`). It first checks that the tag, `pyproject.toml` and `__version__` agree.
4. It then runs the full CI workflow, the same checks as every PR. Its `package` job builds the viewer (which also writes `THIRD_PARTY_LICENSES.txt`), builds the wheel and sdist, smoke-tests both, and uploads them as the `dist` artifact.
5. The `pypi` job waits for a maintainer to approve the `pypi` environment. Once approved, it downloads that `dist` artifact (nothing is rebuilt) and publishes it to PyPI with attestations through trusted publishing.

Push only the tag you are releasing (`git push origin vX.Y.Z`, never `git push --tags`).
