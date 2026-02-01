# GitLab Container Registry

GitLab's built-in container registry is enabled for storing Docker images per-project.

## Endpoints

- **Registry URL:** `gitlab-registry.lab.nkontur.com`
- **GitLab URL:** `gitlab.lab.nkontur.com`

## Usage

### Login

Use a GitLab personal access token with `read_registry` and `write_registry` scopes:

```bash
docker login gitlab-registry.lab.nkontur.com
# Username: your GitLab username
# Password: your personal access token
```

### Push Images

Images are namespaced per-project:

```bash
# Tag your image with the registry path
docker tag myimage:latest gitlab-registry.lab.nkontur.com/<group>/<project>:latest

# Push to registry
docker push gitlab-registry.lab.nkontur.com/<group>/<project>:latest
```

Example for the homelab repo:
```bash
docker tag my-custom-image:v1 gitlab-registry.lab.nkontur.com/root/homelab/my-custom-image:v1
docker push gitlab-registry.lab.nkontur.com/root/homelab/my-custom-image:v1
```

### Pull Images

```bash
docker pull gitlab-registry.lab.nkontur.com/<group>/<project>:<tag>
```

### CI/CD Integration

In `.gitlab-ci.yml`:

```yaml
build:
  stage: build
  image: docker:latest
  services:
    - docker:dind
  variables:
    DOCKER_TLS_CERTDIR: "/certs"
  before_script:
    - docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY
  script:
    - docker build -t $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
```

The CI environment automatically provides:
- `$CI_REGISTRY` = `gitlab-registry.lab.nkontur.com`
- `$CI_REGISTRY_USER` = `gitlab-ci-token`
- `$CI_REGISTRY_PASSWORD` = auto-generated token
- `$CI_REGISTRY_IMAGE` = `gitlab-registry.lab.nkontur.com/<group>/<project>`

### Viewing Images

1. Navigate to your project in GitLab
2. Go to **Deploy > Container Registry**
3. Browse available images and tags

## Architecture

```
[Docker Client]
       │
       ▼ (HTTPS :443)
[lab_nginx reverse proxy]
       │
       ▼ (HTTP :5050)
[GitLab Container Registry]
       │
       ▼
[/var/opt/gitlab/gitlab-rails/shared/registry]
```

- SSL is terminated at the nginx reverse proxy
- Registry runs on port 5050 inside the GitLab container
- Images are stored in the GitLab data volume

## Configuration Files

- **Registry config:** `docker/gitlab/gitlab.rb`
- **Nginx proxy:** `docker/nginx/http-internal-drop-in.conf`
- **Docker compose:** `docker/docker-compose.yml` (port 5050)

## Comparison with Standalone Registry

This homelab also has a standalone Docker Distribution registry at `registry.lab.nkontur.com` on the mgmt network. The differences:

| Feature | GitLab Registry | Standalone Registry |
|---------|-----------------|---------------------|
| URL | gitlab-registry.lab.nkontur.com | registry.lab.nkontur.com |
| Network | internal (10.3.x.x) | mgmt (10.4.x.x) |
| Auth | GitLab accounts/tokens | htpasswd file |
| Project integration | Per-project namespacing | Flat namespace |
| Use case | CI/CD, project artifacts | Infrastructure images |

## Troubleshooting

### Registry Not Responding

1. Check if GitLab container is healthy:
   ```bash
   docker ps | grep gitlab
   docker logs gitlab 2>&1 | tail -50
   ```

2. Verify registry is running inside GitLab:
   ```bash
   docker exec gitlab gitlab-ctl status registry
   ```

3. Check nginx proxy logs:
   ```bash
   docker logs lab_nginx 2>&1 | grep registry
   ```

### Authentication Errors

1. Ensure your token has correct scopes: `read_registry`, `write_registry`
2. Verify the registry URL doesn't have a trailing slash
3. Try logging out and back in:
   ```bash
   docker logout gitlab-registry.lab.nkontur.com
   docker login gitlab-registry.lab.nkontur.com
   ```

### Push/Pull Failures

1. Check image name format matches: `gitlab-registry.lab.nkontur.com/<group>/<project>/<image>:<tag>`
2. Verify project visibility settings allow registry access
3. Check disk space on the router host
