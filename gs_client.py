"""Stateless GeoServer HTTP client.

Design goals
  * NO login / session / cookie handling. Every request is independent.
  * Authentication is optional and preemptive (Authorization: Basic header on each
    request), or delegated to a QGIS Authentication Configuration.
  * Never calls global, admin-only endpoints (/rest/about/version, /rest/workspaces
    listing, /rest/security/*, ...). Only workspace-scoped paths are used.
  * Every request and response is logged in detail (secrets redacted).
"""
import base64
import json
import time
from urllib.parse import quote

from qgis.core import QgsBlockingNetworkRequest, QgsSettings
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from . import gsp_log as L

_BODY_LOG_LIMIT = 4000

_HINTS = {
    0: "No HTTP response at all. Check the URL (include /geoserver), network/VPN/proxy "
       "(QGIS Settings > Options > Network), firewall, and TLS certificates.",
    301: "Redirect. If it goes http->https the body of PUT/POST may be dropped by the server/proxy; "
         "use the final https URL directly.",
    302: "Redirect. Use the final URL directly (often http->https or a login page redirect).",
    400: "Bad request. The server rejected the payload (bad zip/shapefile, bad XML, bad name). "
         "Read the response body below.",
    401: "Unauthorized: credentials missing or wrong, or this endpoint does not allow anonymous "
         "access. Check username/password. A user who is only workspace-admin still gets 401/403 on "
         "global endpoints.",
    403: "Forbidden: credentials accepted (or anonymous) but this user has no permission for this "
         "operation/resource. Needs a role with ADMIN rights on this workspace in "
         "Security > Data (workspace.*.a) and REST access allowed for /rest/workspaces/<ws>/** in "
         "Security > Services/REST. Also may be CSRF/Origin/Referer filtering by a proxy.",
    404: "Not found. Wrong base URL (missing /geoserver?), workspace/store/layer does not exist, "
         "or REST is disabled/hidden for this user (GeoServer returns 404 for resources a user may not see).",
    405: "Method not allowed. A reverse proxy may block PUT/POST/DELETE, or REST is disabled.",
    409: "Conflict: resource already exists (store/layer/style with that name).",
    413: "Payload too large: raise the proxy/server upload limit (nginx client_max_body_size, etc.).",
    415: "Unsupported media type: the Content-Type was not accepted for this endpoint.",
    500: "Server error. GeoServer logs have the cause (often: invalid data, missing plugin such as the "
         "GeoPackage extension, CRS not found, or write permission on the data directory).",
    502: "Bad gateway from a proxy in front of GeoServer.",
    503: "Service unavailable (GeoServer starting up/overloaded).",
    504: "Gateway timeout from a proxy; large uploads may need a higher timeout.",
}

_NET_ERRORS = {}
for _n in dir(QNetworkReply):
    _v = getattr(QNetworkReply, _n)
    if isinstance(_n, str) and _n.endswith("Error") and not _n.startswith("_"):
        try:
            _NET_ERRORS[int(_v)] = _n
        except Exception:
            pass


class Response:
    def __init__(self):
        self.status = 0
        self.headers = {}
        self.body = b""
        self.net_error = None      # QNetworkReply error name
        self.error_string = ""
        self.elapsed = 0.0
        self.url = ""
        self.method = ""

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        try:
            return self.body.decode("utf-8", "replace")
        except Exception:
            return repr(self.body[:200])

    def json(self):
        return json.loads(self.text)

    def brief(self):
        return "%s %s -> HTTP %s" % (self.method, self.url, self.status)


class GeoServerClient:
    def __init__(self, base_url, username="", password="", auth_cfg="", timeout_s=120):
        base_url = (base_url or "").strip().rstrip("/")
        # Accept ".../geoserver/web", ".../geoserver/rest" etc. pasted by the user
        for suffix in ("/web", "/rest", "/ows", "/wms", "/wfs"):
            if base_url.lower().endswith(suffix):
                L.warn("Base URL ended with '%s'; stripping it." % suffix)
                base_url = base_url[: -len(suffix)]
        self.base_url = base_url
        self.username = username or ""
        self.password = password or ""
        self.auth_cfg = auth_cfg or ""
        self.timeout_s = timeout_s
        L.info("Client created: base_url=%s user=%r auth_cfg=%r timeout=%ss (no session/login is used)"
               % (self.base_url, self.username or "<anonymous>", self.auth_cfg or "<none>", timeout_s))

    # ------------------------------------------------------------------ core
    def request(self, method, path, data=None, content_type=None, accept=None,
                query=None, ok_statuses=None):
        """Perform ONE independent HTTP request. Never raises for HTTP errors."""
        url = path if path.startswith("http") else self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + "&".join(
                "%s=%s" % (k, quote(str(v), safe="")) for k, v in query.items())
        req = QNetworkRequest(QUrl(url))
        headers = {}
        if accept:
            headers["Accept"] = accept
        if content_type:
            headers["Content-Type"] = content_type
        if self.username:
            tok = base64.b64encode(("%s:%s" % (self.username, self.password)).encode("utf-8")).decode()
            headers["Authorization"] = "Basic " + tok   # preemptive: no 401 challenge round-trip
        headers["User-Agent"] = "QGIS-GeoServerPublisher/0.1"
        for k, v in headers.items():
            req.setRawHeader(k.encode(), v.encode())
        try:
            req.setTransferTimeout(int(self.timeout_s * 1000))
        except Exception:
            pass
        try:
            req.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                             QNetworkRequest.NoLessSafeRedirectPolicy)
        except Exception:
            pass

        body = data if data is not None else b""
        if isinstance(body, str):
            body = body.encode("utf-8")

        L.debug(">>> REQUEST %s %s" % (method, url))
        for k, v in headers.items():
            L.debug(">>>   %s: %s" % (k, v if k != "Authorization" else "Basic ***REDACTED***"))
        L.debug(">>>   body: %d bytes%s" % (len(body), self._peek(body, content_type)))

        blocking = QgsBlockingNetworkRequest()
        if self.auth_cfg:
            blocking.setAuthCfg(self.auth_cfg)

        resp = Response()
        resp.method, resp.url = method, url
        t0 = time.time()
        try:
            qd = QByteArray(body)
            if method == "GET":
                err = blocking.get(req, True)
            elif method == "POST":
                err = blocking.post(req, qd, True)
            elif method == "PUT":
                # put() has no forceRefresh argument (unlike get/post/head): put(req, data, feedback=None)
                err = blocking.put(req, qd)
            elif method == "DELETE":
                err = blocking.deleteResource(req)
            elif method == "HEAD":
                err = blocking.head(req, True)
            else:
                raise ValueError("Unsupported method " + method)
        except Exception:
            L.exception("Exception while sending %s %s" % (method, url))
            resp.error_string = "exception while sending (see log)"
            return resp
        resp.elapsed = time.time() - t0

        reply = blocking.reply()
        try:
            st = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            resp.status = int(st) if st is not None else 0
        except Exception:
            resp.status = 0
        try:
            for k in reply.rawHeaderList():
                kb = bytes(k)
                resp.headers[kb.decode("latin-1")] = bytes(reply.rawHeader(kb)).decode("latin-1")
        except Exception:
            L.exception("Could not read response headers (non-fatal)")
        resp.body = bytes(reply.content())
        if err != QgsBlockingNetworkRequest.NoError or not resp.ok:   # Qt reports "Unknown error" even on success; don't log that
            resp.error_string = blocking.errorMessage() or reply.errorString() or ""
        try:
            resp.net_error = _NET_ERRORS.get(int(reply.error()), str(int(reply.error())))
        except Exception:
            pass

        lvl = "DEBUG" if resp.ok else "ERROR"
        L.log(lvl, "<<< RESPONSE %s %s -> HTTP %s in %.2fs (blocking err=%s, qt err=%s)"
              % (method, url, resp.status, resp.elapsed, err, resp.net_error))
        for k, v in resp.headers.items():
            L.log(lvl, "<<<   %s: %s" % (k, v))
        if resp.error_string:
            L.log(lvl, "<<<   error string: %s" % resp.error_string)
        if resp.body:
            txt = resp.text
            more = "" if len(txt) <= _BODY_LOG_LIMIT else "\n... [truncated, %d chars total]" % len(txt)
            L.log(lvl, "<<<   body (%d bytes):\n%s%s" % (len(resp.body), txt[:_BODY_LOG_LIMIT], more))
        else:
            L.log(lvl, "<<<   body: <empty>")

        if not resp.ok and not (ok_statuses and resp.status in ok_statuses):
            self._explain(resp)
        return resp

    @staticmethod
    def _peek(body, content_type):
        if not body or not content_type:
            return ""
        if "xml" in content_type or "json" in content_type or "text" in content_type:
            s = body[:1500].decode("utf-8", "replace")
            return "\n" + s + ("\n...[truncated]" if len(body) > 1500 else "")
        return " (binary, %s)" % content_type

    def _explain(self, resp):
        L.error("FAILED: %s" % resp.brief())
        hint = _HINTS.get(resp.status) or _HINTS.get(resp.status // 100 * 100)
        if resp.status == 0:
            hint = _HINTS[0]
        if hint:
            L.error("HINT: " + hint)
        if 'Protocol ""' in (resp.error_string or ""):
            L.error("HINT: the server answered with a redirect to a relative URL that QGIS could not "
                    "follow. The server IS reachable; this endpoint redirects (e.g. /web/ or a login "
                    "page). Check the base URL, or call the endpoint the redirect points to.")
        if "text/html" in resp.headers.get("Content-Type", "") and resp.status in (401, 403, 404):
            L.error("HINT: response is an HTML page, so this is probably a proxy/login page or the "
                    "web UI, not the REST API. Verify the base URL.")
        www = resp.headers.get("WWW-Authenticate")
        if www:
            L.error("HINT: server challenged with WWW-Authenticate: %s" % www)
        if not self.username and not self.auth_cfg and resp.status in (401, 403):
            L.error("HINT: no credentials were sent. Enter a username/password of a (non-admin) "
                    "workspace user, or choose a QGIS auth configuration.")

    # ------------------------------------------------------- workspace-scoped
    def probe_root(self):
        """Reachability check, no auth needed. Any HTTP answer proves the server is up.

        Uses WFS GetCapabilities rather than /web/: /web/ answers 302 with a host-relative
        Location (/geoserver/web/?0) and QgsBlockingNetworkRequest follows it unresolved,
        failing with 'Protocol "" is unknown'.
        """
        return self.request("GET", "/ows", ok_statuses=(401, 403),
                            query={"service": "WFS", "version": "1.1.0",
                                   "request": "GetCapabilities"})

    def get_workspace(self, ws):
        return self.request("GET", "/rest/workspaces/%s.json" % quote(ws), accept="application/json")

    def list_datastores(self, ws):
        return self.request("GET", "/rest/workspaces/%s/datastores.json" % quote(ws),
                            accept="application/json")

    def get_featuretype(self, ws, store, name):
        return self.request(
            "GET", "/rest/workspaces/%s/datastores/%s/featuretypes/%s.json"
            % (quote(ws), quote(store), quote(name)), accept="application/json", ok_statuses=(404,))

    def upload_vector_file(self, ws, store, data, ext, configure="all"):
        """PUT a zipped shapefile (ext='shp') or a geopackage (ext='gpkg') into a (new or existing) store."""
        ctype = "application/zip" if ext == "shp" else "application/octet-stream"
        return self.request(
            "PUT", "/rest/workspaces/%s/datastores/%s/file.%s" % (quote(ws), quote(store), ext),
            data=data, content_type=ctype, accept="application/json",
            query={"configure": configure})

    def create_style(self, ws, name, sld_xml):
        return self.request(
            "POST", "/rest/workspaces/%s/styles" % quote(ws), data=sld_xml,
            content_type="application/vnd.ogc.sld+xml", query={"name": name})

    def update_style(self, ws, name, sld_xml):
        return self.request(
            "PUT", "/rest/workspaces/%s/styles/%s" % (quote(ws), quote(name)), data=sld_xml,
            content_type="application/vnd.ogc.sld+xml")

    def set_default_style(self, ws, layer, style_ws, style):
        xml = ("<layer><defaultStyle><name>%s:%s</name></defaultStyle></layer>" % (style_ws, style))
        return self.request(
            "PUT", "/rest/workspaces/%s/layers/%s" % (quote(ws), quote(layer)),
            data=xml, content_type="application/xml")

    # ------------------------------------------------------------- OGC (WFS-T)
    def wfs_describe(self, ws, layer):
        return self.request("GET", "/%s/ows" % quote(ws), accept="application/xml",
                            query={"service": "WFS", "version": "1.1.0",
                                   "request": "DescribeFeatureType", "typeName": "%s:%s" % (ws, layer)})

    def wfs_transaction(self, ws, xml):
        return self.request("POST", "/%s/wfs" % quote(ws), data=xml,
                            content_type="text/xml", accept="text/xml")
