# -*- coding: utf-8 -*-
"""
HTTP 服务：REST API + SSE 实时推送 + 静态页面。仅标准库。
"""
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import Core, CoreError
from portal import Portal, PortalError

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA_PATH = os.path.join(BASE, "..", "data", "state.json")

CHAOS = {"lose_receipt": False, "lose_notify": False}
portal = Portal(CHAOS)
core = Core(portal, DATA_PATH)


def scheduler():
    while True:
        try:
            core.tick()
        except Exception as e:
            print("[scheduler] error: %r" % e)
        time.sleep(0.5)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------

    def _send(self, status, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message, extra=None):
        obj = {"error": {"code": code, "message": message}}
        if extra:
            obj["error"].update(extra)
        self._send(status, obj)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _user(self):
        auth = self.headers.get("Authorization") or ""
        token = auth.replace("Bearer ", "", 1).strip()
        return core.user_of(token)

    def log_message(self, *a):
        pass

    # ---------- 路由 ----------

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path == "/api/events":
                return self._sse()
            if path == "/api/state":
                return self._send(200, core.state(self._user()))
            if path.startswith("/api/files/"):
                # <a> 标签无法带 Authorization 头，允许 ?token= 传递
                from urllib.parse import urlparse, parse_qs
                qtoken = parse_qs(urlparse(self.path).query).get("token", [None])[0]
                if qtoken:
                    core.user_of(qtoken)
                else:
                    self._user()
                return self._download_file(path.rsplit("/", 1)[-1])
            if path == "/" or path == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if path == "/style.css":
                return self._static("style.css", "text/css; charset=utf-8")
            return self._error(404, "NOT_FOUND", "not found")
        except CoreError as e:
            return self._error(e.status, e.code, e.message, e.extra)
        except Exception as e:
            return self._error(500, "INTERNAL", repr(e))

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            body = self._body()

            if path == "/api/login":
                token = core.login(body.get("user", ""), body.get("password", ""))
                return self._send(200, {"token": token})

            user = self._user()   # 以下均需登录

            if path == "/api/tasks":
                tid = core.create_task(user, body.get("name"), body.get("org"),
                                       body.get("period"), body.get("caliber"))
                return self._send(200, {"task_id": tid})
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")   # api/tasks/<id>/<action>
                tid, action = parts[2], parts[3] if len(parts) > 3 else ""
                return self._task_action(user, tid, action, body)
            if path == "/api/chaos":
                core.set_chaos(body.get("key"), body.get("value"))
                return self._send(200, {"ok": True})
            if path == "/api/chaos/expire_sessions":
                n = core.expire_sessions(user)
                return self._send(200, {"expired": n})
            if path == "/api/portal/free_page":
                return self._send(200, {"page": core.open_free_page(user)})
            if path == "/api/portal/free_page_action":
                page = core.free_page_action(user, body.get("page_id"),
                                             body.get("field"), body.get("value"))
                return self._send(200, {"page": page})
            return self._error(404, "NOT_FOUND", "not found")
        except CoreError as e:
            return self._error(e.status, e.code, e.message, e.extra)
        except PortalError as e:
            return self._error(e.status, e.code, e.message)
        except Exception as e:
            return self._error(500, "INTERNAL", repr(e))

    def _task_action(self, user, tid, action, body):
        if action == "takeover":
            core.takeover(user, tid, force=bool(body.get("force")))
        elif action == "handback":
            core.handback(user, tid)
        elif action == "page":
            core.page_action(user, tid, body.get("field"), body.get("value"))
        elif action == "submit":
            core.human_submit(user, tid)
        elif action == "nudge":
            core.nudge(user, tid, body.get("job_id"))
        elif action == "download":
            core.download(user, tid, body.get("job_id"))
        elif action == "reconcile":
            core.reconcile_now(user, tid)
        elif action == "relogin_portal":
            core.relogin_portal(user, tid)
        else:
            return self._error(404, "NOT_FOUND", "unknown action")
        return self._send(200, {"ok": True})

    # ---------- 输出 ----------

    def _download_file(self, file_id):
        f = portal.get_file(file_id)
        if not f:
            return self._error(404, "NOT_FOUND", "file not found")
        body = f["content"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         "attachment; filename=%s" % f["name"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = queue.Queue()
        core.subscribe(q)
        try:
            while True:
                try:
                    q.get(timeout=15)
                    self.wfile.write(b"data: refresh\n\n")
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            core.unsubscribe(q)


def main():
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    core.restore()
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "8321"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("采集工作台已启动: http://127.0.0.1:%d" % port)
    print("虚构账号: alice/alice123, bob/bob123（门户机器人 bot-01 由后台自动登录）")
    srv.serve_forever()


if __name__ == "__main__":
    main()
