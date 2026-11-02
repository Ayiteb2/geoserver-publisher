def classFactory(iface):
    from .plugin import GeoServerPublisherPlugin
    return GeoServerPublisherPlugin(iface)
