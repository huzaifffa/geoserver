# GIS stack: GeoServer + PostGIS on Dokploy

```
git push ──► Dokploy clones repo ──► docker compose up
                                       ├─ postgis     (volume: postgis_data)
                                       ├─ geoserver   (volume: geoserver_data, ./data mounted read-only at /data)
                                       └─ publisher   one-shot: run.py registers every file in ./data
```

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The stack Dokploy deploys |
| `run.py` | Publisher: walks `data/` and creates GeoServer stores/layers through the REST API |
| `data/` | GIS source files, versioned in git |
| `compose.local.yml` | Port mappings for local testing only |
| `.env.example` | All settings; copy to `.env` locally, or paste into Dokploy's Environment tab |

## How data gets published

Add files under `data/`, commit, push, and redeploy. The publisher runs on each deploy:

| File | Published as |
|---|---|
| `.shp` (+ `.dbf .shx .prj`) | Shapefile store, read directly from `/data` |
| `.gpkg` | GeoPackage store, one layer per table |
| `.tif` / `.tiff` | GeoTIFF coverage |
| `.geojson` / `.json` | Loaded into PostGIS (`ogr2ogr -overwrite`), then published from the `postgis` store |

- Layer names are the lowercased file name, in workspace `gis` (for example `gis:aoi`). If two files share a name, the later one gets a suffix (`_pg`, `_tif`, …).
- An `.sld` file next to a dataset with the same name (for example `aoi.sld`) becomes that layer's default style.
- Re-running is safe: existing layers are kept. Edited files are picked up because GeoServer reads them live and the publisher resets its caches. GeoJSON tables are reloaded on every run.
- Deleting a file does **not** remove its layer. Remove it in the GeoServer UI (Layers, then Stores).
- Hidden files and folders (`.DS_Store`, `__MACOSX`) are skipped.

Check the result in Dokploy: open the `publisher` container's logs. It exits with code 1 if any file failed.

### Large files and git

GitHub rejects files over 100 MB and warns above 50 MB. `data/roads/tl_2023_us_primaryroads.geojson` (82 MB) is within the limit, but each edit adds another full copy to the repo history. For big datasets, prefer compact formats such as GeoPackage or Shapefile over GeoJSON. The roads data is in the repo twice (as a Shapefile and as GeoJSON), so you can drop one copy. For anything over 100 MB, keep it out of git: put it in a host directory and add a second read-only bind mount.

## Deploy on Dokploy

1. Push this folder to a GitHub repo.
2. In Dokploy, choose **Create Service**, then **Compose**. Set the provider to GitHub, pick the repo and branch, and set the compose path to `./docker-compose.yml`.
3. In the **Environment** tab, paste the contents of `.env.example` with real values. Set strong passwords, and set `GEOSERVER_DOMAIN` and `PROXY_BASE_URL` to your real host.
4. In the **Domains** tab, add your host (for example `gis.example.com`) with service `geoserver`, port `8080`, and HTTPS on.
5. Click **Deploy**. GeoServer is then at `https://gis.example.com/geoserver`.
6. Optional: turn on **Autodeploy** (GitHub webhook) so every push redeploys.

Persistence: `geoserver_data` holds everything set in the GeoServer UI (styles, security, extra layers, tile cache), and `postgis_data` holds the database. Both survive redeploys. Back them up with Dokploy's volume backups or `pg_dump`. Never run `docker compose down -v` on the server.

PostGIS has no public port. From QGIS or psql, use an SSH tunnel: `ssh -L 5432:<postgis-container-ip>:5432 user@vps`. You can also add a `ports:` entry restricted with a firewall rule.

## Local testing

```bash
cp .env.example .env    # set GEOSERVER_DOMAIN=localhost:8080, PROXY_BASE_URL=http://localhost:8080/geoserver
docker compose -f docker-compose.yml -f compose.local.yml up -d
docker compose logs -f publisher
# UI: http://localhost:8080/geoserver  (admin / GEOSERVER_ADMIN_PASSWORD)
```

## Using the services

Replace `https://gis.example.com` with your domain.

| Service | Endpoint |
|---|---|
| WMS | `https://gis.example.com/geoserver/gis/wms` |
| WFS | `https://gis.example.com/geoserver/gis/wfs` |
| WMTS / tiles | `https://gis.example.com/geoserver/gwc/service/wmts` |

**QGIS:** In the Browser panel, right-click WMS/WMTS (or WFS / OGC API - Features), choose New Connection, and paste the endpoint.

**Leaflet (WMS):**

```js
L.tileLayer.wms("https://gis.example.com/geoserver/gis/wms", {
  layers: "gis:tl_2023_us_primaryroads",
  format: "image/png",
  transparent: true,
}).addTo(map);
```

**Leaflet (WFS as GeoJSON):**

```js
fetch("https://gis.example.com/geoserver/gis/ows?service=WFS&version=2.0.0&request=GetFeature" +
      "&typeNames=gis:aoi&outputFormat=application/json&srsName=EPSG:4326")
  .then(r => r.json())
  .then(data => L.geoJSON(data).addTo(map));
```

**OpenLayers (WMS):**

```js
new ol.layer.Tile({
  source: new ol.source.TileWMS({
    url: "https://gis.example.com/geoserver/gis/wms",
    params: { LAYERS: "gis:tl_2023_us_primaryroads", TILED: true },
    serverType: "geoserver",
  }),
});
```

Once the web apps exist, set `CORS_ALLOWED_ORIGINS` to their origins instead of `*`.
