# GeoServer Publisher (QGIS plugin)

Publish QGIS vector layers to GeoServer without needing an admin account.

- **No REST login or session.** Each request sends its own Basic auth header, or none for anonymous access. A QGIS authentication configuration also works.
- **Workspace-scoped only.** It calls only `/rest/workspaces/<ws>/...` endpoints and never global admin-only ones, so a user who is admin of one workspace is enough.
- **Detailed logging.** Every request and response is logged with headers, timing and body. Passwords, auth headers and cookies are redacted. Failures add plain-English hints per HTTP status.

## Modes

| Mode | What it does | Needs |
|---|---|---|
| REST: shapefile | Exports the layer, zips it, uploads it, creates or replaces the store and layer | REST access to the workspace |
| REST: GeoPackage | Same, as a GeoPackage (keeps field names longer than 10 characters) | REST access, plus GeoPackage support on the server |
| WFS-T append | Adds features to an **existing** layer | WFS-T write access only, no REST |

Options: selected features only, and uploading the QGIS style as SLD (set as the layer's default style).

## Install

1. Download `geoserver_publisher.zip` from Releases, or zip the `geoserver_publisher` folder yourself.
2. In QGIS: **Plugins > Manage and Install Plugins > Install from ZIP**.
3. Open it from **Web > GeoServer Publisher** or the toolbar button.

Requires QGIS 3.16 or newer. Tested on QGIS 3.44 with GeoServer 3.0.2.

## GeoServer setup for a non-admin user

By default GeoServer allows REST only for `ROLE_ADMINISTRATOR`, so a workspace user gets **403 Forbidden** even when they can manage the workspace in the web admin. Two settings are needed.

**1. Data security** (Security > Data, or `<data dir>/security/layers.properties`): give a role admin rights on the workspace.

```
ne.*.a=editor_role
```

**2. REST security** (`<data dir>/security/rest.properties`): add these lines **above** the existing catch-all rules, then restart GeoServer.

```
/rest/workspaces/ne*;GET,HEAD,OPTIONS=ROLE_ADMINISTRATOR,editor_role
/rest/workspaces/ne/**;GET,HEAD,OPTIONS,POST,PUT,DELETE=ROLE_ADMINISTRATOR,editor_role
```

Replace `ne` and `editor_role` with your workspace and role. On Windows, saving that file usually needs an editor running as administrator.

Use **Test connection** to check: the workspace read and datastore list should return HTTP 200.

## Logs

- In the dialog's log pane
- In the QGIS message log, tab "GeoServer Publisher"
- In a file: `<QGIS profile>/geoserver_publisher/geoserver_publisher.log` (the **Open log file** button opens it)

## Known limits

- Shapefile field names are cut to 10 characters. Use GeoPackage to avoid this.
- WFS-T is append-only, needs the target layer to exist, and the source must use the same CRS as the target.
- SLD export from QGIS is experimental and may not look identical in GeoServer.
- Publishing runs on the main thread, so QGIS freezes during large uploads.
- "Remember password" stores the password unencrypted in QGIS settings. Prefer a QGIS auth configuration.
