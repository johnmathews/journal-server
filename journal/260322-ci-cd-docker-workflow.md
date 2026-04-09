# CI/CD: Add Docker build and push to ghcr.io

Renamed `.github/workflows/ci.yml` to `ci-and-deploy.yml` and added a `build-and-push` job
that builds the Docker image and pushes it to `ghcr.io/johnmathews/journal-server`.

The new job:
- Only runs on pushes to `main` (not on PRs)
- Depends on the `test` job passing first
- Authenticates with `GITHUB_TOKEN` via `docker/login-action@v3`
- Tags images with `latest` and the short commit SHA
- Uses `docker/metadata-action@v5` for label/tag extraction and `docker/build-push-action@v6` for the build
