import socket
import logging
import random

import gevent
import dpkt

from direct import Proxy
from http_connect import HttpConnectProxy


LOGGER = logging.getLogger(__name__)


class DynamicProxy(Proxy):
    def __init__(self, proxy_dns_record):
        self.proxy_dns_record = proxy_dns_record
        self.delegated_to = None
        super(DynamicProxy, self).__init__()

    def forward(self, client):
        if self.delegated_to:
            self.delegated_to.forward(client)
        else:
            raise NotImplementedError()

    @property
    def died(self):
        if self.delegated_to:
            return self.delegated_to.died
        else:
            return False

    @died.setter
    def died(self, value):
        if self.delegated_to:
            self.delegated_to.died = value
        else:
            pass # ignore

    @classmethod
    def refresh(cls, proxies, create_sock):
        greenlets = []
        for proxy in proxies:
            greenlets.append(gevent.spawn(resolve_proxy, proxy))
        for greenlet in greenlets:
            greenlet.join()

    def is_protocol_supported(self, protocol):
        if self.delegated_to:
            return self.delegated_to.is_protocol_supported(protocol)
        else:
            return False

    def __repr__(self):
        if self.delegated_to:
            return 'DynamicProxy[%s=>%s]' % (self.proxy_dns_record, repr(self.delegated_to))
        else:
            return 'DynamicProxy[UNRESOLVED]'


def resolve_proxy(proxy):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP
        sock.settimeout(3)
        request = dpkt.dns.DNS(
            id=random.randint(1, 65535), qd=[dpkt.dns.DNS.Q(name=proxy.proxy_dns_record, type=dpkt.dns.DNS_TXT)])
        sock.sendto(str(request), ('8.8.8.8', 53))
        connection_info = dpkt.dns.DNS(sock.recv(1024)).an[0].rdata
        connection_info = ''.join(e for e in connection_info if e.isalnum() or e in [':', '.', '-'])
        proxy_type, ip, port, username, password = connection_info.split(':')
        assert 'http-connect' == proxy_type # only support one type currently
        proxy.delegated_to = HttpConnectProxy(ip, port)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('resolved proxy: %s' % repr(proxy))
    except:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('failed to resolve proxy: %s' % repr(proxy), exc_info=1)
        proxy.delegated_to = None