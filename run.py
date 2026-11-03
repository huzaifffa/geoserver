"""Publish everything under DATA_DIR to GeoServer.

Runs inside the `publisher` container on each deploy. Uses only the standard
library plus ogr2ogr (shipped in the GDAL image).

  .shp      -> Shapefile store, read straight from /data
  .gpkg     -> GeoPackage store, one layer per table
  .tif/.tiff-> GeoTIFF coverage store
  .geojson / .json -> loaded into PostGIS with ogr2ogr, then published
                      from the shared "postgis" store

A sibling <name>.sld file becomes that layer's default style.
Existing stores and layers are left alone, so re-running is safe.
"""

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

GEOSERVER_URL = os.environ.get("GEOSERVER_URL", "http://geoserver:8080/geoserver").rstrip("/")
REST = f"{GEOSERVER_URL}/rest"
WORKSPACE = os.environ.get("GEOSERVER_WORKSPACE", "gis")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "").strip()
POSTGIS_STORE = "postgis"

_auth = base64.b64encode(
    f"{os.environ.get('GEOSERVER_ADMIN_USER', 'admin')}:{os.environ['GEOSERVER_ADMIN_PASSWORD']}".encode()
).decode()


def request(method, path, body=None, content_type="application/json", ok=(200, 201)):
    """Call the GeoServer REST API. Returns (status, parsed JSON or text)."""
    data = None
    if body is not None:
        data = json.dumps(body).encode() if content_type == "application/json" else body.encode()
    req = urllib.request.Request(f"{REST}{path}", data=data, method=method)
    req.add_header("Authorization", f"Basic {_auth}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            status, raw = resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode(errors="replace")
    if status not in ok:
        raise RuntimeError(f"{method} {path} -> {status}: {raw[:500]}")
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def exists(path):
    status, _ = request("GET", path, ok=(200, 404))
    return status == 200


def slug(name):
    """GeoServer/PostGIS-safe lowercase identifier."""
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_").lower()
    return s if s and not s[0].isdigit() else f"l_{s}"


def as_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


# --- GeoServer setup ---------------------------------------------------------

def ensure_workspace():
    if not exists(f"/workspaces/{WORKSPACE}"):
        request("POST", "/workspaces", {"workspace": {"name": WORKSPACE}})
        print(f"created workspace {WORKSPACE}")


def configure_global_settings():
    _, settings = request("GET", "/settings")
    s = settings["global"]["settings"]
    # Default is 4 decimals (~10 m) in WFS/GeoJSON output; 8 is ~1 mm.
    s["numDecimals"] = 8
    # Make capabilities documents advertise the public URL, not the container's.
    if PROXY_BASE_URL:
        s["proxyBaseUrl"] = PROXY_BASE_URL
    request("PUT", "/settings", settings)
    print(f"settings: numDecimals=8, proxyBaseUrl={PROXY_BASE_URL or '(unset)'}")


def ensure_datastore(name, params):
    if exists(f"/workspaces/{WORKSPACE}/datastores/{name}"):
        return
    entries = [{"@key": k, "$": str(v)} for k, v in params.items()]
    request(
        "POST",
        f"/workspaces/{WORKSPACE}/datastores",
        {"dataStore": {"name": name, "connectionParameters": {"entry": entries}}},
    )
    print(f"created datastore {name}")


def publish_featuretype(store, native_name, layer_name):
    if exists(f"/layers/{WORKSPACE}:{layer_name}"):
        print(f"  = {WORKSPACE}:{layer_name} (exists)")
        return False
    request(
        "POST",
        f"/workspaces/{WORKSPACE}/datastores/{store}/featuretypes",
        {"featureType": {"name": layer_name, "nativeName": native_name}},
    )
    print(f"  + {WORKSPACE}:{layer_name}")
    return True


def available_featuretypes(store):
    _, body = request("GET", f"/workspaces/{WORKSPACE}/datastores/{store}/featuretypes.json?list=available")
    return as_list((body or {}).get("list", {}).get("string")) if isinstance(body, dict) else []


def apply_sld(source, layer_name):
    sld = source.with_suffix(".sld")
    if not sld.exists():
        return
    style_path = f"/workspaces/{WORKSPACE}/styles/{layer_name}"
    if not exists(style_path):
        request("POST", f"/workspaces/{WORKSPACE}/styles", {"style": {"name": layer_name, "filename": f"{layer_name}.sld"}})
    request("PUT", style_path, sld.read_text(), content_type="application/vnd.ogc.sld+xml")
    request("PUT", f"/layers/{WORKSPACE}:{layer_name}",
            {"layer": {"defaultStyle": {"name": f"{WORKSPACE}:{layer_name}"}}})
    print(f"  ~ style {sld.name}")


# --- Per-format handlers -----------------------------------------------------

def publish_shapefile(path, layer_name):
    store = f"shp_{layer_name}"
    ensure_datastore(store, {"url": f"file:{path}", "charset": "UTF-8"})
    publish_featuretype(store, path.stem, layer_name)


def publish_geopackage(path, layer_name, used):
    store = f"gpkg_{layer_name}"
    ensure_datastore(store, {"dbtype": "geopkg", "database": str(path), "read_only": "true"})
    # Only tables not yet published; already-published ones are left alone.
    tables = available_featuretypes(store)
    for table in sorted(tables):
        name = unique(slug(table) if len(tables) > 1 else layer_name, used, "gpkg")
        publish_featuretype(store, table, name)


def publish_geotiff(path, layer_name):
    store = f"tif_{layer_name}"
    if exists(f"/layers/{WORKSPACE}:{layer_name}"):
        print(f"  = {WORKSPACE}:{layer_name} (exists)")
        return
    request(
        "PUT",
        f"/workspaces/{WORKSPACE}/coveragestores/{store}/external.geotiff?configure=first&coverageName={layer_name}",
        str(path),  # bare path; GeoServer rejects a file: URL here
        content_type="text/plain",
    )
    print(f"  + {WORKSPACE}:{layer_name}")


def publish_geojson(path, layer_name):
    """Load into PostGIS (overwriting, so git changes propagate) then publish."""
    pg = "PG:host={PGHOST} port={PGPORT} dbname={PGDATABASE} user={PGUSER}".format(**os.environ)
    subprocess.run(
        [
            "ogr2ogr", "-f", "PostgreSQL", pg, str(path),
            "-nln", layer_name, "-overwrite",
            "-nlt", "PROMOTE_TO_MULTI",
            "-lco", "GEOMETRY_NAME=geom",
            "-lco", "FID=gid",
            "--config", "PG_USE_COPY", "YES",
        ],
        check=True,
    )
    print(f"  loaded {path.name} into PostGIS table {layer_name}")
    publish_featuretype(POSTGIS_STORE, layer_name, layer_name)


def ensure_postgis_store():
    ensure_datastore(POSTGIS_STORE, {
        "dbtype": "postgis",
        "host": os.environ["PGHOST"],
        "port": os.environ.get("PGPORT", "5432"),
        "database": os.environ["PGDATABASE"],
        "user": os.environ["PGUSER"],
        "passwd": os.environ["PGPASSWORD"],
        "schema": "public",
        "Expose primary keys": "true",
        "validate connections": "true",
    })


# --- Main --------------------------------------------------------------------

HANDLERS = {
    ".shp": "shp",
    ".gpkg": "gpkg",
    ".tif": "tif",
    ".tiff": "tif",
    ".geojson": "pg",
    ".json": "pg",
}


def unique(name, used, kind):
    """Layer names share one namespace per workspace; suffix clashes by source type."""
    if name in used:
        name = f"{name}_{kind}"
    used.add(name)
    return name


def discover():
    files = []
    for path in sorted(DATA_DIR.rglob("*")):
        if any(part.startswith((".", "__MACOSX")) for part in path.relative_to(DATA_DIR).parts):
            continue
        kind = HANDLERS.get(path.suffix.lower())
        if path.is_file() and kind:
            files.append((path, kind))
    return files


def main():
    ensure_workspace()
    configure_global_settings()

    files = discover()
    if any(kind == "pg" for _, kind in files):
        ensure_postgis_store()

    used, failed = set(), []
    for path, kind in files:
        print(f"{path.relative_to(DATA_DIR)}")
        try:
            if kind == "gpkg":
                publish_geopackage(path, slug(path.stem), used)
                continue
            layer_name = unique(slug(path.stem), used, kind)
            {"shp": publish_shapefile, "tif": publish_geotiff, "pg": publish_geojson}[kind](path, layer_name)
            apply_sld(path, layer_name)
        except Exception as e:  # keep going so one bad file doesn't block the rest
            failed.append(path)
            print(f"  ! {e}", file=sys.stderr)

    # Drop cached file handles so replaced files in git are picked up.
    request("POST", "/reset")
    print(f"done: {len(files) - len(failed)} ok, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
