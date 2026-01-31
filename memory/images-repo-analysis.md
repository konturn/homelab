# Images Repo Pattern Analysis

**Repo:** `root/images` (Project ID 8)
**Cloned to:** `/home/node/clawd/images-repo`

## Pattern Summary

### CI Structure (`.gitlab-ci.yml`)
```yaml
.build:  # Template
  stage: build
  image: docker:20.10.16
  services:
    - name: docker:20.10.16-dind
      command: ["--insecure-registry=registry.lab.nkontur.com"]
  script:
    - docker login -u docker -p ${DOCKER_REGISTRY_KEY} registry.lab.nkontur.com
    - docker build -f $DOCKERFILE . -t registry.lab.nkontur.com/${NAME}
    - docker push registry.lab.nkontur.com/${NAME}

# Each image = 4 lines extending template
snapcast:
  extends: .build
  variables:
    DOCKERFILE: Dockerfile.snapcast
    NAME: snapcast
```

### Key Features
- **Docker-in-Docker (dind)** with insecure registry for local builds
- **Template inheritance** - adding new image is 4 lines
- **Naming convention:** `Dockerfile.<name>` → `registry.lab.nkontur.com/<name>`
- **No conditional builds** - rebuilds on every push

### Images Built
- `snapcast` - Snapserver with librespot (Spotify Connect) + shairport-sync (AirPlay)
- `snapclient` - Snapcast client

---

## Homelab Comparison

### Current Homelab CI Pattern
- Uses **Kaniko** (not dind) for building CI runner image
- **Conditional builds** - only rebuilds when deps change
- Only builds ONE image: the CI runner itself

### Homelab Dockerfiles (not built in CI)
1. `ci/Dockerfile` - CI runner image (built via Kaniko)
2. `docker/moltbot/Dockerfile` - Custom moltbot with tooling (NOT BEING BUILT)

### Images Consumed from registry.lab.nkontur.com
- snapcast (7 instances in docker-compose)
- snapclient
- amcrest2mqtt (no Dockerfile found)

---

## Extensibility Options

### Option 1: Keep Separation (Recommended)
- Move `docker/moltbot/Dockerfile` to images repo
- Create amcrest2mqtt Dockerfile in images repo
- Images repo = image factory, homelab = consumer

**Pros:** Clean separation, images repo is single source of truth
**Cons:** Changes span two repos

### Option 2: Consolidate into Homelab
- Add image-building stage to homelab CI
- Use same dind pattern from images repo
- All Dockerfiles live with the compose that uses them

**Pros:** Everything in one place
**Cons:** Mixes concerns, longer CI runs

### Option 3: Hybrid
- Keep CI image build in homelab (Kaniko, conditional)
- Keep application images in images repo
- Currently the de facto pattern

---

## Gap: moltbot Image

The `docker/moltbot/Dockerfile` exists but has no CI building it. Either:
1. Move to images repo and add build job
2. Add build job to homelab CI
3. Build manually (current state, fragile)

---

*Analysis date: 2026-01-31*
