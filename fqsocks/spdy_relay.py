import logging
import socket
import tlslite
import gevent
import gevent.queue
import spdy.context
import spdy.frames
import select
import sys
import base64
from http_try import recv_and_parse_request

from direct import Proxy


LOGGER = logging.getLogger(__name__)


class SpdyClient(object):
    create_tcp_socket = None

    def __init__(self, ip, port):
        self.sock = self.create_tcp_socket(ip, port, 3)
        self.tls_conn = tlslite.TLSConnection(self.sock)
        self.tls_conn.handshakeClientCert(nextProtos=['spdy/3'])
        assert 'spdy/3' == self.tls_conn.next_proto
        self.spdy_context = spdy.context.Context(spdy.context.CLIENT, version=3)
        self.window_size = 65536
        self.current_window = 0
        self.streams = {}


    def open_stream(self, headers):
        stream_id = self.spdy_context.next_stream_id
        self.streams[stream_id] = gevent.queue.Queue()
        self.send(spdy.frames.SynStream(stream_id, headers, version=3))
        return stream_id

    def loop(self):
        try:
            while True:
                select.select([self.sock], [], [])
                data = self.tls_conn.read()
                self.spdy_context.incoming(data)
                self.consume_frames()
        except:
            LOGGER.exception('spdy loop failed')


    def consume_frames(self):
        while True:
            frame = self.spdy_context.get_frame()
            if not frame:
                return
            if isinstance(frame, spdy.frames.Settings):
                LOGGER.info('received settings: %s' % frame)
                window_size_settings = dict(frame.id_value_pairs).get(spdy.frames.INITIAL_WINDOW_SIZE)
                if window_size_settings:
                    self.window_size = window_size_settings[1]
            if isinstance(frame, spdy.frames.DataFrame):
                self.current_window += len(frame.data)
                if self.current_window >= self.window_size / 2:
                    self.send(spdy.frames.WindowUpdate(frame.stream_id, self.window_size))
            if hasattr(frame, 'stream_id'):
                self.streams[frame.stream_id].put(frame)


    def send(self, frame):
        self.spdy_context.put_frame(frame)
        data = self.spdy_context.outgoing()
        self.tls_conn.write(data)


    def close(self):
        try:
            self.tls_conn.close()
            self.tls_conn = None
        except:
            pass
        try:
            self.sock.close()
            self.sock = None
        except:
            pass


class SpdyRelayProxy(Proxy):
    def __init__(self, proxy_ip, proxy_port, username=None, password=None, is_public=False):
        super(SpdyRelayProxy, self).__init__()
        self.proxy_ip = socket.gethostbyname(proxy_ip)
        self.proxy_port = proxy_port
        self.username = username
        self.password = password
        self.spdy_client = None
        if is_public:
            self.flags.add('PUBLIC')

    def connect(self):
        try:
            self.close()
            self.spdy_client = SpdyClient(self.proxy_ip, self.proxy_port)
            gevent.spawn(self.spdy_client.loop)
        except:
            LOGGER.exception('failed to connect spdy-relay proxy: %s' % self)
            self.died = True

    def close(self):
        if self.spdy_client:
            self.spdy_client.close()

    def do_forward(self, client):
        recv_and_parse_request(client)
        headers = {
            ':method': client.method,
            ':scheme': 'http',
            ':path': client.path,
            ':version': 'HTTP/1.1',
            ':host': client.host
        }
        if self.username and self.password:
            auth = base64.b64encode('%s:%s' % (self.username, self.password)).strip()
            headers['proxy-authorization'] = 'Basic %s\r\n' % auth
        for k, v in client.headers.items():
            headers[k.lower()] = v
        stream_id = self.spdy_client.open_stream(headers)
        stream = self.spdy_client.streams[stream_id]
        remaining_bytes = sys.maxint
        while remaining_bytes > 0:
            try:
                frame = stream.get(timeout=10)
            except gevent.queue.Empty:
                if client.forward_started:
                    raise Exception('taking too long to read frame from proxy')
                else:
                    return client.fall_back('no response from proxy')
            if isinstance(frame, spdy.frames.SynReply):
                remaining_bytes = self.on_syn_reply_frame(client, frame)
            elif isinstance(frame, spdy.frames.DataFrame):
                client.forward_started = True
                remaining_bytes -= self.on_data_frame(client, frame)
            else:
                LOGGER.warn('[%s] unknown frame: %s' % (repr(client), frame))

    def on_syn_reply_frame(self, client, frame):
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('[%s] syn reply: %s' % (repr(client), frame.headers))
        headers = dict(frame.headers)
        http_version = headers.pop(':version')
        status = headers.pop(':status')
        client.forward_started = True
        client.downstream_sock.sendall('%s %s\r\n' % (http_version, status))
        for k, v in headers.items():
            client.downstream_sock.sendall('%s: %s\r\n' % (k, v))
        client.downstream_sock.sendall('\r\n')
        if status.startswith('304'):
            return 0
        else:
            return int(headers.pop('content-length', sys.maxint))

    def on_data_frame(self, client, frame):
        client.downstream_sock.sendall(frame.data)
        return len(frame.data)


    @classmethod
    def refresh(cls, proxies, create_udp_socket, create_tcp_socket):
        for proxy in proxies:
            proxy.connect()
        return True

    def is_protocol_supported(self, protocol):
        return protocol == 'HTTP'

    def __repr__(self):
        return 'SpdyRelayProxy[%s:%s]' % (self.proxy_ip, self.proxy_port)
