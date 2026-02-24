# Headway Maps Setup

Headway provides a unified self-hosted maps experience with search (Pelias geocoding),
routing (Valhalla via Travelmux), and map tiles — all behind a single web frontend.

**URL:** https://maps.lab.nkontur.com
**Geocoding API:** https://pelias.lab.nkontur.com

## Architecture

We reuse the existing **tileserver** and **valhalla** containers (already configured with
US data). Headway adds:

- **headway-frontend** — web UI at maps.lab.nkontur.com
- **travelmux** — multi-modal routing proxy wrapping our existing Valhalla
- **pelias** stack — geocoding (replaces Nominatim): elasticsearch, api, placeholder, libpostal

## Data Build (Required Before First Start)

Headway uses [Dagger](https://docs.dagger.io/install) to build data artifacts (map tiles,
geocoding indices, routing data). This must be done once on a machine with Docker and Dagger.

### Option A: Custom OSM extract (recommended for Cleveland area)

```bash
# On a build machine with Dagger installed
git clone https://github.com/headwaymaps/headway.git
cd headway

# Download a regional OSM extract
wget https://download.geofabrik.de/north-america/us/ohio-latest.osm.pbf

# Copy a template build config
cp -r builds/Bogota builds/Cleveland
# Edit builds/Cleveland/.env — set HEADWAY_AREA=Cleveland

# Build with custom PBF
bin/build builds/Cleveland --local-pbf ./ohio-latest.osm.pbf

# The build outputs data artifacts to data/Cleveland/
```

### Option B: Pre-configured city (if available)

```bash
# If your city is in the supported list:
cp -r builds/Bogota builds/YourCity
bin/build builds/YourCity
```

### Deploy artifacts to server

Copy the built data to the server:

```bash
rsync -avz data/Cleveland/ server:/persistent_data/application/headway/data/Cleveland/
```

The docker-compose init containers will read from this directory on first start.

## DNS

Add DNS records for:
- `maps.lab.nkontur.com` → router IP
- `pelias.lab.nkontur.com` → router IP

## Cleanup After Migration

After verifying Headway is working:

1. Remove Nominatim data: `rm -rf /persistent_data/application/nominatim/`
2. Remove the Nominatim vault secret at `services/nominatim`
3. Update any automation that referenced `nominatim.lab.nkontur.com` to use
   `pelias.lab.nkontur.com` or `maps.lab.nkontur.com`

## Rebuilding Data

If you need to update the map data or change the area:

```bash
# On the server
docker compose down headway-frontend headway-frontend-init travelmux headway-travelmux-init \
  pelias-config-init pelias-elasticsearch-init pelias-placeholder-init

# Remove init volumes to force re-initialization
docker volume rm homelab_headway_frontend_data homelab_headway_travelmux_data \
  homelab_headway_pelias_config homelab_headway_pelias_elasticsearch homelab_headway_pelias_placeholder

# Copy new data artifacts, then bring services back up
docker compose up -d
```

## Resource Usage

Approximate additional memory (on top of existing tileserver + valhalla):
- pelias-elasticsearch: 2-4 GB
- pelias-libpostal: 2-4 GB
- pelias-api: ~128 MB
- pelias-placeholder: ~128 MB
- headway-frontend: ~128 MB
- travelmux: ~256 MB
- **Total additional: ~5-9 GB**
