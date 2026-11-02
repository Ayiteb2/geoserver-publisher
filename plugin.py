import os

from qgis.core import QgsMapLayerProxyModel, QgsSettings, QgsApplication
from qgis.gui import QgsAuthConfigSelect, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (QAction, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                                 QSpinBox, QVBoxLayout, QApplication)

from . import gsp_log as L
from .gs_client import GeoServerClient
from . import publisher

S = "GeoServerPublisher/"


class PublishDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Publish to GeoServer")
        self.resize(780, 760)
        st = QgsSettings()

        self.url = QLineEdit(st.value(S + "url", "http://localhost:8080/geoserver"))
        self.user = QLineEdit(st.value(S + "user", ""))
        self.user.setPlaceholderText("leave empty for anonymous access")
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox("Remember password (stored UNENCRYPTED in QGIS settings)")
        if st.value(S + "remember", False, type=bool):
            self.remember.setChecked(True)
            self.pwd.setText(st.value(S + "pwd", ""))
        self.authcfg = QgsAuthConfigSelect()
        cfg = st.value(S + "authcfg", "")
        if cfg:
            self.authcfg.setConfigId(cfg)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setValue(int(st.value(S + "timeout", 300)))
        self.timeout.setSuffix(" s")

        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(QgsMapLayerProxyModel.VectorLayer)
        self.mode = QComboBox()
        self.mode.addItem("REST upload - create/replace store + layer (shapefile)", "rest_shp")
        self.mode.addItem("REST upload - create/replace store + layer (GeoPackage ext. required)", "rest_gpkg")
        self.mode.addItem("WFS-T append to an EXISTING layer (no REST access needed)", "wfst")
        self.ws = QLineEdit(st.value(S + "ws", ""))
        self.store = QLineEdit()
        self.store.setPlaceholderText("default: layer name")
        self.name = QLineEdit()
        self.name.setPlaceholderText("default: layer name (sanitised)")
        self.selected = QCheckBox("Selected features only")
        self.style = QCheckBox("Also upload the QGIS style as SLD (experimental)")

        form = QFormLayout()
        form.addRow("GeoServer URL", self.url)
        form.addRow("Username", self.user)
        form.addRow("Password", self.pwd)
        form.addRow("", self.remember)
        form.addRow("or QGIS auth config", self.authcfg)
        form.addRow("Timeout", self.timeout)
        form.addRow(QLabel("<hr>"))
        form.addRow("QGIS layer", self.layer)
        form.addRow("Mode", self.mode)
        form.addRow("Workspace", self.ws)
        form.addRow("Store (REST)", self.store)
        form.addRow("Layer name", self.name)
        form.addRow("", self.selected)
        form.addRow("", self.style)

        self.logview = QPlainTextEdit()
        self.logview.setReadOnly(True)
        self.logview.setMaximumBlockCount(20000)
        self.logview.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.logview.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")

        b_test = QPushButton("Test connection")
        b_pub = QPushButton("Publish")
        b_logf = QPushButton("Open log file")
        b_clear = QPushButton("Clear view")
        b_test.clicked.connect(self.on_test)
        b_pub.clicked.connect(self.on_publish)
        b_logf.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(L.log_file_path())))
        b_clear.clicked.connect(self.logview.clear)
        row = QHBoxLayout()
        for b in (b_test, b_pub, b_logf, b_clear):
            row.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row)
        lay.addWidget(QLabel("Log (full detail also in the QGIS 'GeoServer Publisher' log tab and %s):"
                             % L.log_file_path()))
        lay.addWidget(self.logview, 1)
        L.add_listener(self._on_log)

    def done(self, r):
        L.remove_listener(self._on_log)
        super().done(r)

    def _on_log(self, level, msg):
        self.logview.appendPlainText("[%s] %s" % (level, msg))
        QApplication.processEvents()

    def _save(self):
        st = QgsSettings()
        st.setValue(S + "url", self.url.text())
        st.setValue(S + "user", self.user.text())
        st.setValue(S + "ws", self.ws.text())
        st.setValue(S + "authcfg", self.authcfg.configId())
        st.setValue(S + "timeout", self.timeout.value())
        st.setValue(S + "remember", self.remember.isChecked())
        st.setValue(S + "pwd", self.pwd.text() if self.remember.isChecked() else "")

    def _client(self):
        if not self.url.text().strip():
            QMessageBox.warning(self, "GeoServer Publisher", "Enter the GeoServer URL.")
            return None
        self._save()
        return GeoServerClient(self.url.text(), self.user.text(), self.pwd.text(),
                               self.authcfg.configId(), self.timeout.value())

    def on_test(self):
        try:
            self._run_test()
        except Exception:
            L.exception("Unhandled exception during connection test")

    def _run_test(self):
        c = self._client()
        if not c:
            return
        ws = self.ws.text().strip()
        L.info("=" * 70)
        L.info("CONNECTION TEST")
        r = c.probe_root()
        if r.status == 0:
            L.error("RESULT: server not reachable.")
            return
        L.info("Server answered HTTP %s on WFS GetCapabilities (reachable)." % r.status)
        if not ws:
            L.warn("Enter a workspace to test the workspace-scoped REST endpoints.")
            return
        r = c.get_workspace(ws)
        L.info("Workspace read: HTTP %s (%s)" % (r.status, "OK" if r.ok else "not available"))
        r = c.list_datastores(ws)
        L.info("Datastore list: HTTP %s (%s)" % (r.status, "OK - REST access works for this user"
                                                  if r.ok else "REST access NOT confirmed"))
        r = c.request("GET", "/%s/ows" % ws, query={"service": "WFS", "request": "GetCapabilities"},
                      ok_statuses=(401, 403))
        L.info("WFS GetCapabilities: HTTP %s (%s)" % (r.status, "OK - WFS-T mode possible" if r.ok else "failed"))

    def on_publish(self):
        c = self._client()
        if not c:
            return
        layer = self.layer.currentLayer()
        ws = self.ws.text().strip()
        if layer is None or not ws:
            QMessageBox.warning(self, "GeoServer Publisher", "Choose a vector layer and enter a workspace.")
            return
        name = publisher.sanitize(self.name.text().strip() or layer.name())
        store = publisher.sanitize(self.store.text().strip() or name)
        mode = self.mode.currentData()
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if mode == "wfst":
                ok = publisher.publish_wfst(c, layer, ws, name, self.selected.isChecked())
            else:
                ok = publisher.publish_rest(
                    c, layer, ws, store, name, "shp" if mode == "rest_shp" else "gpkg",
                    self.selected.isChecked(), self.style.isChecked(), name)
        except Exception:
            L.exception("Unhandled exception during publish")
            ok = False
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)
        if ok:
            self.iface.messageBar().pushSuccess("GeoServer Publisher", "Published %s:%s" % (ws, name))
        else:
            self.iface.messageBar().pushCritical(
                "GeoServer Publisher", "Publish failed - see the log in the dialog / log file.")


class GeoServerPublisherPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Publish to GeoServer...", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToWebMenu("GeoServer Publisher", self.action)
        self.iface.addToolBarIcon(self.action)
        L.info("Plugin loaded. Log file: " + L.log_file_path())

    def unload(self):
        self.iface.removePluginWebMenu("GeoServer Publisher", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        self.dlg = PublishDialog(self.iface, self.iface.mainWindow())
        self.dlg.show()
