import json
import os
import socket
import time

from saichallenger.common.sai_client.sai_redis_client.sai_redis_client import (
    SaiRedisClient,
)


class SaiZmqClient(SaiRedisClient):
    """ZMQ SAI client. Reuses Redis for VID/ASIC_DB/counters; SAI RPC goes over ZMQ."""

    DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"
    DEFAULT_TIMEOUT_MS = 60 * 1000
    SWITCH_CREATE_TIMEOUT_MS = 180 * 1000

    def __init__(self, cfg):
        super().__init__(cfg)
        import zmq

        self._zmq = zmq
        self.zmq_endpoint = cfg.get("zmq_endpoint", self.DEFAULT_ENDPOINT)
        self.zmq_timeout_ms = int(cfg.get("zmq_timeout_ms", self.DEFAULT_TIMEOUT_MS))
        self._zmq_ctx = zmq.Context()
        self._req = None
        self._open_req_socket()

        # SaiRedisClient uses name-mangled __check/__assert helpers. Rebind them
        # so cleanup(), warm reboot, and operate() liveness use ZMQ, not Redis PUBSUB.
        self._SaiRedisClient__check_syncd_running = self.__check_syncd_running
        self._SaiRedisClient__assert_syncd_running = self.__assert_syncd_running

    def cleanup(self):
        super().cleanup()
        # Redis shutdown restarts syncd; the old REQ socket is no longer valid.
        self._open_req_socket()

    def deinit(self):
        """Release the ZMQ context. A live context keeps its I/O and reaper
        threads running and blocks the interpreter from exiting."""
        self._close_req_socket()
        if self._zmq_ctx is not None:
            self._zmq_ctx.destroy(linger=0)
            self._zmq_ctx = None

    def operate(self, obj, attrs, op):
        if self.asic_channel is None:
            self.__assert_syncd_running()

        # Remove spaces from the key string.
        # Required by sai_deserialize_route_entry() in sonic-sairedis.
        obj = obj.replace(' ', '')
        if "bv_id" in obj:
            obj = obj.replace("bv_id", "bvid")
            obj = obj.replace("mac_address", "mac")

        # Required by sai_deserialize_neighbor_entry() in sonic-sairedis.
        if "ip_address" in obj:
            obj = obj.replace("ip_address", "ip")
            obj = obj.replace("rif_id", "rif")

        cmd = op[1:] if op and op[0] in ("S", "D") else op
        payload = [obj, cmd] + self._attrs_to_fv_list(attrs)

        timeout_ms = self.zmq_timeout_ms
        if obj.startswith("SAI_OBJECT_TYPE_SWITCH") and cmd == "create":
            timeout_ms = max(timeout_ms, self.SWITCH_CREATE_TIMEOUT_MS)

        reply = self._req_rep(payload, timeout_ms)
        assert reply is not None and len(reply) >= 2, f"SAI \"{cmd}\" operation failure!"
        return self._reply_as_redis_status(reply)

    def __check_syncd_running(self):
        if self._peer_reachable():
            return self.zmq_endpoint
        return None

    def __assert_syncd_running(self, tout=30):
        for i in range(tout + 1):
            self.asic_channel = self.__check_syncd_running()
            if self.asic_channel:
                return
            if i < tout:
                time.sleep(1)
        assert False, "SyncD has not started yet..."

    def _open_req_socket(self):
        self._close_req_socket()
        if self._zmq_ctx is None:
            self._zmq_ctx = self._zmq.Context()
        self._req = self._zmq_ctx.socket(self._zmq.REQ)
        self._req.setsockopt(self._zmq.LINGER, 0)
        self._req.connect(self.zmq_endpoint)

    def _close_req_socket(self):
        if self._req is not None:
            self._req.close(linger=0)
            self._req = None

    def _req_rep(self, payload, timeout_ms):
        zmq = self._zmq
        msg = json.dumps(payload, separators=(',', ':'))
        self._req.setsockopt(zmq.SNDTIMEO, int(timeout_ms))
        self._req.setsockopt(zmq.RCVTIMEO, int(timeout_ms))
        try:
            self._req.send_string(msg)
            raw = self._req.recv_string()
        except zmq.Again:
            self._open_req_socket()
            return None
        except zmq.ZMQError:
            assert self.__check_syncd_running(), "FATAL - SyncD has exited or crashed!"
            self._open_req_socket()
            raise

        reply = json.loads(raw)
        assert isinstance(reply, list), f"Unexpected ZMQ reply: {raw}"
        return reply

    def _peer_reachable(self):
        ep = self.zmq_endpoint
        try:
            if ep.startswith("tcp://"):
                rest = ep[len("tcp://"):]
                if rest.startswith("["):
                    host, port = rest[1:].split("]:", 1)
                else:
                    host, port = rest.rsplit(":", 1)
                if host in ("*", "0.0.0.0", "::"):
                    host = "127.0.0.1"
                with socket.create_connection((host, int(port)), timeout=1):
                    return True
            if ep.startswith("ipc://"):
                return os.path.exists(ep[len("ipc://"):])
        except (OSError, ValueError):
            return False
        return False

    @staticmethod
    def _attrs_to_fv_list(attrs):
        if attrs is None:
            return []
        data = json.loads(attrs) if isinstance(attrs, str) else attrs
        if isinstance(data, dict) or data is None:
            return []
        if not isinstance(data, list):
            return []
        return [item if isinstance(item, str) else json.dumps(item) for item in data]

    @staticmethod
    def _reply_as_redis_status(reply):
        """Map ZMQ [status, op, f, v, ...] to Redis GETRESPONSE [Sop, attrs_json, status]."""
        sai_status, resp_op = reply[0], reply[1]
        attrs_json = json.dumps(reply[2:])
        return [
            ("S" + resp_op).encode("utf-8"),
            attrs_json.encode("utf-8"),
            sai_status.encode("utf-8") if isinstance(sai_status, str) else sai_status,
        ]
