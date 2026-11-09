"""无硬件测试：只编译 ScanClient AST，不导入 Tk/设备代码或启动 MainWindow。"""
import ast
from pathlib import Path
import queue
import re
import socket
import threading
import time
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "device_control.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
CLASS = next(node for node in TREE.body
             if isinstance(node, ast.ClassDef) and node.name == "ScanClient")
NAMESPACE = {"socket": socket, "threading": threading, "re": re,
             "SCAN_READY": "NETSCAN_SERVER_READY"}
exec(compile(ast.Module(body=[CLASS], type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)
ScanClient = NAMESPACE["ScanClient"]


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待条件超时")


def read_line(sock):
    data = b""
    while not data.endswith(b"\n"):
        part = sock.recv(1)
        if not part:
            raise AssertionError(f"连接提前关闭: {data!r}")
        data += part
    return data


def socket_proxy(factory):
    return types.SimpleNamespace(socket=factory, AF_INET=socket.AF_INET,
                                 SOCK_STREAM=socket.SOCK_STREAM,
                                 SHUT_RDWR=socket.SHUT_RDWR, timeout=socket.timeout)


class ScanClientTests(unittest.TestCase):
    def setUp(self):
        self.states = []
        self.ready_calls = []
        self.responses = queue.Queue()
        self.client = ScanClient(
            lambda connected, msg: self.states.append((connected, msg)),
            lambda: self.ready_calls.append(True), self.responses.put)
        self.sockets = []

    def tearDown(self):
        self.client.close()
        for sock in self.sockets:
            sock.close()
        thread = self.client._listener_thread
        if thread:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "监听线程未退出")

    def server(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(2)
        self.sockets.append(server)
        return server

    def outgoing(self):
        server = self.server()
        self.assertTrue(self.client.connect(*server.getsockname()))
        peer, _ = server.accept()
        peer.settimeout(2)
        self.sockets.append(peer)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        return peer

    def listener(self):
        self.assertTrue(self.client.start_listener("127.0.0.1", 0))
        return self.client._listener.getsockname()

    def incoming(self, address):
        peer = socket.create_connection(address, timeout=2)
        self.sockets.append(peer)
        return peer

    def assert_no_response(self):
        with self.assertRaises(queue.Empty):
            self.responses.get(timeout=0.05)

    def test_outgoing_hello_ready_and_commands(self):
        peer = self.outgoing()
        peer.sendall(b"OK HELLO PLC\r\nNETSCAN_SERVER_READY\r\n")
        self.assertEqual(self.responses.get(timeout=2), "NETSCAN_SERVER_READY")
        self.assertTrue(self.client.ready)
        self.assertEqual(self.ready_calls, [True])
        self.assertTrue(self.client.send("FLASH\r\n"))
        self.assertEqual(read_line(peer), b"FLASH\r\n")
        peer.sendall(b"OK FLASH BEFORE=OFF AFTER=ON\r\n")
        self.assertEqual(self.responses.get(timeout=2), "OK FLASH BEFORE=OFF AFTER=ON")
        self.assert_no_response()

    def test_incoming_hello_ready_and_same_command_channel(self):
        peer = self.incoming(self.listener())
        # MFC 先发 READY，HELLO 的 OK 之后不再重复 READY。
        peer.sendall(b"NETSCAN_SERVER_READY\r\n")
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        peer.sendall(b"OK HELLO PLC\r\n")
        self.assertEqual(self.responses.get(timeout=2), "NETSCAN_SERVER_READY")
        self.assertTrue(self.client.send("PRESS_INK 2\r\n"))
        self.assertEqual(read_line(peer), b"PRESS_INK 2\r\n")
        self.assertEqual([state for state, _ in self.states], [True])
        self.assert_no_response()

    def test_ping_before_ready_and_internal_frames_never_dispatch(self):
        peer = self.incoming(self.listener())
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        peer.sendall(b"OK HELLO PLC\r\nPING TOK")
        peer.sendall(b"EN=000123\r\nPING TOKEN=987654321\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=000123\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=987654321\r\n")
        peer.sendall(b"PING TOKEN=0\r\nPING TOKEN=-1\r\nPING TOKEN=x\r\n"
                     b"PING TOKEN=1 EVENT START_JOB TOTAL_LAYERS=1\r\n"
                     b"PONG TOKEN=123\r\nPING\r\nPONG\r\n")
        self.assert_no_response()
        self.assertFalse(self.client.ready)
        self.assertEqual(self.ready_calls, [])

    def test_outgoing_ping_and_event_fragmentation_preserves_lines(self):
        peer = self.outgoing()
        first = "EVENT START_JOB TOTAL_LAYERS=3"
        second = "EVENT PASS_READY CURRENT=1 TOTAL=2 STEP=-1715 EMPTY=0"
        third = "EVENT LAYER_START LAYER=2"
        peer.sendall((first + "\r\n" + second[:24]).encode("ascii"))
        self.assertEqual(self.responses.get(timeout=2), first)
        self.assert_no_response()  # 半行不能触发任务
        peer.sendall((second[24:] + "\r\nPING TOKEN=7\r\n" + third + "\n").encode("ascii"))
        self.assertEqual(read_line(peer), b"PONG TOKEN=7\r\n")
        self.assertEqual(self.responses.get(timeout=2), second)
        self.assertEqual(self.responses.get(timeout=2), third)
        self.assert_no_response()

    def test_ready_requires_complete_line_and_notifies_once(self):
        peer = self.outgoing()
        peer.sendall(b"NETSCAN_SERVER_READY")
        self.assert_no_response()
        self.assertFalse(self.client.ready)
        peer.sendall(b"\r\nNETSCAN_SERVER_READY\r\n")
        self.assertEqual(self.responses.get(timeout=2), "NETSCAN_SERVER_READY")
        self.assertEqual(self.responses.get(timeout=2), "NETSCAN_SERVER_READY")
        self.assertEqual(self.ready_calls, [True])

    def test_existing_connection_rejects_incoming_without_disconnect_callback(self):
        peer = self.outgoing()
        original = self.client.sock
        duplicate = self.incoming(self.listener())
        self.assertEqual(duplicate.recv(1), b"")
        self.assertIs(self.client.sock, original)
        self.assertEqual([state for state, _ in self.states], [True])
        peer.sendall(b"EVENT LAYER_START LAYER=1\r\n")
        self.assertEqual(self.responses.get(timeout=2), "EVENT LAYER_START LAYER=1")
        self.assert_no_response()

    def test_existing_incoming_connection_skips_outgoing_socket_creation(self):
        peer = self.incoming(self.listener())
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        factory = mock.Mock(side_effect=AssertionError("不得创建第二条连接"))
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(factory)):
            self.assertTrue(self.client.connect("127.0.0.1", 19090))
        factory.assert_not_called()
        wait_for(lambda: len(self.states) == 1)
        self.assertEqual([state for state, _ in self.states], [True])

    def test_incoming_wins_while_outgoing_is_connecting(self):
        address = self.listener()
        server = self.server()
        started, release = threading.Event(), threading.Event()
        temporary = socket.socket()

        class GatedSocket:
            def __getattr__(self, name):
                return getattr(temporary, name)

            def connect(self, endpoint):
                started.set()
                if not release.wait(2):
                    raise OSError("测试连接超时")
                temporary.connect(endpoint)

        factory = mock.Mock(return_value=GatedSocket())
        results = []
        worker = threading.Thread(target=lambda: results.append(
            self.client.connect(*server.getsockname())))
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(factory)):
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                # 另一个主动 connect 也不能再创建临时 socket。
                self.assertFalse(self.client.connect(*server.getsockname()))
                peer = self.incoming(address)
                self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
                wait_for(lambda: len(self.states) == 1)
                self.assertEqual(temporary.fileno(), -1)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        factory.assert_called_once()
        self.assertTrue(self.client.connected)
        self.assertEqual([state for state, _ in self.states], [True])
        peer.sendall(b"PING TOKEN=19\r\nEVENT LAYER_START LAYER=1\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=19\r\n")
        self.assertEqual(self.responses.get(timeout=2), "EVENT LAYER_START LAYER=1")
        self.assert_no_response()

    def test_old_recv_thread_cannot_disconnect_new_connection(self):
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        old = mock.Mock()

        def delayed_recv(size):
            entered.set()
            release.wait(2)
            exited.set()
            return b""

        old.recv.side_effect = delayed_recv
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=old))):
            self.assertTrue(self.client.connect("127.0.0.1", 1))
        self.assertTrue(entered.wait(2))
        self.client.disconnect()
        try:
            peer = self.outgoing()
            current = self.client.sock
            previous_states = list(self.states)
            # 真正运行旧 recv 的 finally（而不只是直接测试清理方法）。
            original_disconnect = self.client._disconnect_socket
            finished = threading.Event()

            def track_disconnect(sock, *args):
                original_disconnect(sock, *args)
                if sock is old:
                    finished.set()

            with mock.patch.object(self.client, "_disconnect_socket", track_disconnect):
                release.set()
                self.assertTrue(finished.wait(2))
            self.assertTrue(exited.is_set())
            self.assertIs(self.client.sock, current)
            self.assertTrue(self.client.connected)
            self.assertEqual(self.states, previous_states)
            peer.sendall(b"PING TOKEN=31\r\n")
            self.assertEqual(read_line(peer), b"PONG TOKEN=31\r\n")
        finally:
            release.set()

    def test_connect_failure_closes_temporary_socket(self):
        temporary = mock.Mock()
        temporary.connect.side_effect = OSError("模拟拒绝连接")
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            self.assertFalse(self.client.connect("127.0.0.1", 1))
        temporary.close.assert_called_once()
        self.assertIsNone(self.client.sock)
        self.assertIsNone(self.client._connecting)
        self.assertEqual([state for state, _ in self.states], [False])

    def test_hello_is_sent_after_install_without_waiting_for_ready(self):
        temporary = mock.Mock()
        installed = []

        def verify_install(data):
            installed.append(self.client.sock is temporary
                             and self.client.connected
                             and not self.client.ready)
            self.assertEqual(data, b"HELLO PLC\r\n")

        temporary.sendall.side_effect = verify_install
        temporary.recv.return_value = b""
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            self.assertTrue(self.client.connect("127.0.0.1", 1))
        self.assertEqual(installed, [True])
        wait_for(lambda: not self.client.connected)

    def test_hello_failure_closes_temporary_socket(self):
        temporary = mock.Mock()
        temporary.sendall.side_effect = OSError("模拟握手发送失败")
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            self.assertFalse(self.client.connect("127.0.0.1", 1))
        temporary.close.assert_called_once()
        self.assertIsNone(self.client.sock)
        self.assertIsNone(self.client._connecting)

    def test_close_stops_listener_and_connection_and_is_idempotent(self):
        address = self.listener()
        listener = self.client._listener
        thread = self.client._listener_thread
        peer = self.incoming(address)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        wait_for(lambda: len(self.states) == 1)
        active = self.client.sock
        self.client.close()
        self.client.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(listener.fileno(), -1)
        self.assertEqual(active.fileno(), -1)
        self.assertEqual(peer.recv(1), b"")
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.client._listener)
        self.assertFalse(self.client.start_listener("127.0.0.1", 0))
        self.assertFalse(self.client.connect("127.0.0.1", 1))
        self.assertEqual([state for state, _ in self.states], [True, False])
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.2)

    def test_manual_disconnect_rejects_reverse_retries_until_connect(self):
        address = self.listener()
        listener = self.client._listener
        peer = self.incoming(address)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        wait_for(lambda: len(self.states) == 1)
        self.client.disconnect()
        self.assertEqual(peer.recv(1), b"")
        self.assertFalse(self.client.connected)
        self.assertFalse(self.client._accepting)
        # 模拟 MFC 多次后台重试：不回 HELLO，不安装连接，不额外回调断开。
        for _ in range(3):
            retry = self.incoming(address)
            self.assertEqual(retry.recv(1), b"")
        self.assertIs(self.client._listener, listener)
        self.assertIsNone(self.client.sock)
        self.assertEqual([state for state, _ in self.states], [True, False])
        # 点击“连接”恢复主动连接及反向接管。
        resumed = self.outgoing()
        self.assertTrue(self.client._accepting)
        resumed.sendall(b"PING TOKEN=71\r\n")
        self.assertEqual(read_line(resumed), b"PONG TOKEN=71\r\n")
        self.assertEqual([state for state, _ in self.states], [True, False, True])

    def test_connect_failure_restores_reverse_acceptance_after_manual_disconnect(self):
        address = self.listener()
        listener = self.client._listener
        self.client.disconnect()
        rejected = self.incoming(address)
        self.assertEqual(rejected.recv(1), b"")
        temporary = mock.Mock()
        temporary.connect.side_effect = OSError("主动连接暂不可用")
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            self.assertFalse(self.client.connect("127.0.0.1", 1))
        temporary.close.assert_called_once()
        self.assertIs(self.client._listener, listener)
        self.assertTrue(self.client._accepting)
        peer = self.incoming(address)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        peer.sendall(b"NETSCAN_SERVER_READY\r\nOK HELLO PLC\r\nPING TOKEN=72\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=72\r\n")
        self.assertEqual(self.responses.get(timeout=2), "NETSCAN_SERVER_READY")
        self.assertTrue(self.client.connected)

    def test_startup_outgoing_failure_does_not_disable_reverse_listener(self):
        address = self.listener()
        temporary = mock.Mock()
        temporary.connect.side_effect = OSError("主动连接失败")
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            self.assertFalse(self.client.connect("127.0.0.1", 1))
        self.assertTrue(self.client._accepting)
        peer = self.incoming(address)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        peer.sendall(b"PING TOKEN=73\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=73\r\n")

    def test_recv_failure_keeps_reverse_acceptance_enabled(self):
        address = self.listener()
        peer = self.incoming(address)
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        wait_for(lambda: len(self.states) == 1)
        peer.shutdown(socket.SHUT_RDWR)
        peer.close()
        wait_for(lambda: len(self.states) == 2)
        self.assertFalse(self.client.connected)
        self.assertTrue(self.client._accepting)
        reconnected = self.incoming(address)
        self.assertEqual(read_line(reconnected), b"HELLO PLC\r\n")
        reconnected.sendall(b"PING TOKEN=74\r\n")
        self.assertEqual(read_line(reconnected), b"PONG TOKEN=74\r\n")
        self.assertEqual([state for state, _ in self.states], [True, False, True])

    def test_close_cancels_pending_outgoing_without_late_disconnect_callback(self):
        started, release = threading.Event(), threading.Event()
        temporary = mock.Mock()
        results = []

        def connecting(endpoint):
            started.set()
            release.wait(2)

        temporary.connect.side_effect = connecting
        worker = threading.Thread(target=lambda: results.append(
            self.client.connect("127.0.0.1", 1)))
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.client.close()
                temporary.close.assert_called()
                self.assertIsNone(self.client._connecting)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertIsNone(self.client.sock)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.states, [])
        temporary.sendall.assert_not_called()

    def test_stop_listener_keeps_existing_connection_and_can_restart(self):
        peer = self.incoming(self.listener())
        self.assertEqual(read_line(peer), b"HELLO PLC\r\n")
        thread = self.client._listener_thread
        self.client.stop_listener()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.client.connected)
        peer.sendall(b"PING TOKEN=51\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=51\r\n")
        self.listener()

    def test_listener_bind_failure_does_not_disconnect_active_connection(self):
        peer = self.outgoing()
        occupied = self.server()
        temporary = socket.socket()
        with mock.patch.dict(NAMESPACE, socket=socket_proxy(mock.Mock(return_value=temporary))):
            with self.assertRaises(OSError):
                self.client.start_listener(*occupied.getsockname())
        self.assertEqual(temporary.fileno(), -1)
        self.assertTrue(self.client.connected)
        self.assertEqual([state for state, _ in self.states], [True])
        peer.sendall(b"PING TOKEN=61\r\n")
        self.assertEqual(read_line(peer), b"PONG TOKEN=61\r\n")


class ScanCallbackTests(unittest.TestCase):
    """验证Tk延迟回调不会把新通道状态覆盖为旧通道状态，不创建窗口。"""

    @classmethod
    def setUpClass(cls):
        window = next(node for node in TREE.body
                      if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
        callbacks = [node for node in window.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_on_scan_state", "_on_scan_ready"}]
        cls.callbacks = {}
        exec(compile(ast.Module(body=callbacks, type_ignores=[]), str(SOURCE), "exec"),
             cls.callbacks)

    def window(self, connected, ready):
        return types.SimpleNamespace(
            scan=types.SimpleNamespace(connected=connected, ready=ready),
            scan_btn=mock.Mock(), scan_ready_var=mock.Mock(),
            _append_log=mock.Mock(), _set_press_ink_repeat=mock.Mock(),
            _press_ink_repeat_enabled=True, _press_ink_repeat_after=None,
            after=lambda delay, callback: callback())

    def test_old_disconnect_callback_keeps_current_connection_ready(self):
        window = self.window(True, True)
        self.callbacks["_on_scan_state"](window, False, "旧通道断开")
        window.scan_btn.config.assert_called_once_with(text="断开")
        window.scan_ready_var.set.assert_called_once_with("就绪 (NETSCAN_SERVER_READY)")
        window._set_press_ink_repeat.assert_not_called()

    def test_old_ready_callback_cannot_mark_disconnected_channel_ready(self):
        window = self.window(False, False)
        self.callbacks["_on_scan_ready"](window)
        window.scan_ready_var.set.assert_not_called()
        window._append_log.assert_not_called()

    def test_old_connected_callback_cannot_hide_current_disconnect(self):
        window = self.window(False, False)
        self.callbacks["_on_scan_state"](window, True, "旧通道已连接")
        window.scan_btn.config.assert_called_once_with(text="连接")
        window.scan_ready_var.set.assert_called_once_with("未连接")
        window._set_press_ink_repeat.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
