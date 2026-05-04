from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from websocket import create_connection

from hata_bot.exceptions import BrowserAutomationError


LOGGER = logging.getLogger("hata_bot.browser.chrome_cdp")


class ChromeCdpFetcher:
    def __init__(self, *, profile_dir: Path, logger: logging.Logger | None = None) -> None:
        self.profile_dir = profile_dir
        self.logger = logger or LOGGER
        self.chrome_binary = self._find_chrome_binary()

    def fetch_html(
        self,
        *,
        url: str,
        ready_expression: str,
        timeout_sec: int = 30,
    ) -> str:
        port = self._find_free_port()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                str(self.chrome_binary),
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-minimized",
                "--window-position=-2400,0",
                "--window-size=1200,1000",
                "--new-window",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        page_socket = None
        try:
            page_socket = self._connect_to_page_socket(port, timeout_sec=timeout_sec)
            self._send(page_socket, "Page.enable")
            self._send(page_socket, "Runtime.enable")
            self._send(page_socket, "Page.navigate", {"url": url})
            return self._wait_for_ready_html(
                page_socket,
                ready_expression=ready_expression,
                timeout_sec=timeout_sec,
            )
        finally:
            if page_socket is not None:
                try:
                    page_socket.close()
                except Exception:
                    pass
            self._stop_process(process)

    def _wait_for_ready_html(self, page_socket, *, ready_expression: str, timeout_sec: int) -> str:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            title = self._evaluate(page_socket, "document.title") or ""
            body_preview = self._evaluate(page_socket, "document.body ? document.body.innerText.slice(0, 2000) : ''") or ""
            combined = f"{title}\n{body_preview}".lower()
            if "кажется, у вас включён vpn" in combined or "cian_waf_block" in combined:
                raise BrowserAutomationError("ЦИАН отклонил загрузку страницы в браузере (WAF/VPN block).")

            is_ready = self._evaluate(page_socket, ready_expression)
            if is_ready:
                html = self._evaluate(page_socket, "document.documentElement.outerHTML")
                if isinstance(html, str) and html.strip():
                    return html

            time.sleep(0.5)

        raise BrowserAutomationError(f"Timed out waiting for browser HTML readiness: {self.chrome_binary}")

    def _connect_to_page_socket(self, port: int, *, timeout_sec: int):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    targets = json.load(response)
                page = next((item for item in targets if item.get("type") == "page"), None)
                if page and page.get("webSocketDebuggerUrl"):
                    return create_connection(
                        page["webSocketDebuggerUrl"],
                        timeout=timeout_sec,
                        origin=f"http://127.0.0.1:{port}",
                    )
            except Exception:
                time.sleep(0.5)

        raise BrowserAutomationError("Could not connect to Chrome DevTools page socket.")

    def _send(self, page_socket, method: str, params: dict | None = None) -> dict:
        if not hasattr(self, "_message_id"):
            self._message_id = 0

        self._message_id += 1
        payload = {"id": self._message_id, "method": method}
        if params is not None:
            payload["params"] = params

        page_socket.send(json.dumps(payload))
        while True:
            response = json.loads(page_socket.recv())
            if response.get("id") == self._message_id:
                return response

    def _evaluate(self, page_socket, expression: str):
        response = self._send(
            page_socket,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
            },
        )
        result = response.get("result", {}).get("result", {})
        return result.get("value")

    def _stop_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _find_chrome_binary() -> Path:
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise BrowserAutomationError("Could not find Chrome or Edge browser binary for Cian fetches.")

