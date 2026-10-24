# Building the manager image in the monorepo

The manager API and UI are built from the pinned source tree in `services/manager/`; the root `deploy/Dockerfile.manager` uses the checked-in npm lockfile and Maven project.

From the repository root:

```bash
docker compose -f deploy/compose.yaml build xiaozhi-esp32-server-web
docker compose -f deploy/compose.yaml up -d xiaozhi-esp32-server-web
```

The image publishes the manager UI/API on host port `16001` (container port `8002`). Runtime database credentials come only from ignored `state/secrets/manager.env`; never add that file or its contents to Git.
