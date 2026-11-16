# -*- coding: utf-8 -*-
"""
虚构报表门户（模拟某企业报表平台）。
只使用标准库，内存态 + 由 core 持久化。

包含：账号、会话（会过期）、页面（表单状态）、异步生成任务、
结果文件、下载完成通知、幂等键去重。
"""
import hashlib
import json
import threading
import time
import uuid

# ---------------- 基础数据（全部虚构） ----------------

ACCOUNTS = {
    "bot-01": {"password": "bot-pass-01", "name": "采集机器人01", "kind": "bot"},
    "alice":  {"password": "alice123",    "name": "艾丽丝(分析师)", "kind": "human"},
    "bob":    {"password": "bob123",      "name": "鲍勃(分析师)",   "kind": "human"},
}

ORGS = [
    {"id": "ORG-001", "name": "华北事业部"},
    {"id": "ORG-002", "name": "华东事业部"},
    {"id": "ORG-003", "name": "华南事业部"},
]
PERIODS = ["2026-09", "2026-10"]
CALIBERS = [
    {"id": "mgmt",  "name": "管理口径"},
    {"id": "legal", "name": "法定口径"},
    {"id": "tax",   "name": "含税口径"},
]

SESSION_TTL = 120.0          # 门户会话 120 秒过期
JOB_MIN_DURATION = 6.0       # 正常生成耗时
JOB_MAX_DURATION = 10.0
HANG_TIMEOUT = 25.0          # 卡死组合 25 秒后判定失败
HANG_AT = 66                 # 卡死时进度停在这里


def _uid(prefix):
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


def cond_hash(conds):
    """条件指纹：用于幂等键与结果归属比对。"""
    canon = json.dumps(
        {k: conds.get(k) for k in ("org", "period", "caliber", "note")},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:8]


def conds_equal(a, b):
    return cond_hash(a or {}) == cond_hash(b or {})


class PortalError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Portal:
    """门户领域对象。线程安全。chaos 开关由 core 注入。"""

    def __init__(self, chaos):
        self.lock = threading.RLock()
        self.chaos = chaos              # dict: lose_receipt / lose_notify
        self.sessions = {}              # token -> session
        self.pages = {}                 # page_id -> page
        self.jobs = {}                  # job_id -> job
        self.files = {}                 # file_id -> file
        self.notifications = []         # 通知记录（含被丢弃的）
        self.idempotency = {}           # key -> job_id

    # ---------------- 会话 ----------------

    def login(self, username, password):
        acc = ACCOUNTS.get(username)
        if not acc or acc["password"] != password:
            raise PortalError(401, "BAD_CREDENTIALS", "账号或密码错误")
        token = _uid("sess")
        with self.lock:
            self.sessions[token] = {
                "token": token, "user": username, "kind": acc["kind"],
                "created": time.time(), "expires": time.time() + SESSION_TTL,
            }
        return self.sessions[token]

    def check_session(self, token):
        with self.lock:
            sess = self.sessions.get(token)
            if not sess:
                raise PortalError(401, "SESSION_EXPIRED", "门户会话不存在或已过期")
            if sess["expires"] < time.time():
                del self.sessions[token]
                raise PortalError(401, "SESSION_EXPIRED", "门户会话已过期，请重新登录")
            return sess

    def expire_all_sessions(self):
        with self.lock:
            n = len(self.sessions)
            self.sessions.clear()
        return n

    # ---------------- 页面 ----------------

    def open_page(self, token, task_id=None):
        """打开一个报表页面。task_id 非空则绑定到采集任务（工作台页面），
        否则为游离页面（人直接上门户浏览，不计入任何任务进度）。"""
        sess = self.check_session(token)
        page_id = _uid("page")
        with self.lock:
            self.pages[page_id] = {
                "id": page_id, "task_id": task_id, "owner": sess["user"],
                "org": None, "period": None, "caliber": None,
                "note": "", "confirmed": False,
                "created": time.time(), "last_action": time.time(),
            }
        return self.pages[page_id]

    def get_page(self, page_id):
        with self.lock:
            page = self.pages.get(page_id)
            if not page:
                raise PortalError(404, "PAGE_NOT_FOUND", "页面不存在")
            return dict(page)

    def set_field(self, token, page_id, field, value):
        self.check_session(token)
        with self.lock:
            page = self.pages.get(page_id)
            if not page:
                raise PortalError(404, "PAGE_NOT_FOUND", "页面不存在")
            if field not in ("org", "period", "caliber", "note", "confirmed"):
                raise PortalError(400, "UNKNOWN_FIELD", "未知控件: %s" % field)
            if field == "org" and value not in [o["id"] for o in ORGS]:
                raise PortalError(400, "BAD_VALUE", "未知组织: %s" % value)
            if field == "period" and value not in PERIODS:
                raise PortalError(400, "BAD_VALUE", "未知账期: %s" % value)
            if field == "caliber" and value not in [c["id"] for c in CALIBERS]:
                raise PortalError(400, "BAD_VALUE", "未知口径: %s" % value)
            page[field] = value
            page["last_action"] = time.time()
            return dict(page)

    @staticmethod
    def required_controls(conds):
        """控件清单会随条件变化：华南事业部 + 法定口径 时新增必填「补充说明」。
        这是自动执行器会遇上的“控件变化”场景。"""
        controls = [
            {"field": "org", "label": "组织", "required": True},
            {"field": "period", "label": "账期", "required": True},
            {"field": "caliber", "label": "口径", "required": True},
        ]
        if conds.get("org") == "ORG-003" and conds.get("caliber") == "legal":
            controls.append({"field": "note", "label": "补充说明", "required": True})
        return controls

    # ---------------- 提交与异步生成 ----------------

    def submit(self, token, page_id, idempotency_key, actor):
        """点击「生成报表」。支持幂等键；chaos.lose_receipt 时动作生效但回执丢失。"""
        self.check_session(token)
        with self.lock:
            # 幂等：同一 key 直接返回已建任务
            if idempotency_key in self.idempotency:
                job = self.jobs[self.idempotency[idempotency_key]]
                return 200, {"job_id": job["id"], "deduplicated": True,
                             "status": job["status"]}

            page = self.pages.get(page_id)
            if not page:
                raise PortalError(404, "PAGE_NOT_FOUND", "页面不存在")
            conds = {"org": page["org"], "period": page["period"],
                     "caliber": page["caliber"], "note": page.get("note") or ""}
            for c in self.required_controls(conds):
                if c["field"] == "note" and not conds["note"].strip():
                    raise PortalError(422, "MISSING_FIELD",
                                      "缺少必填控件「%s」" % c["label"])
            if not (conds["org"] and conds["period"] and conds["caliber"]):
                raise PortalError(422, "INCOMPLETE", "组织/账期/口径未选齐")

            job_id = _uid("job")
            hang = (conds["period"] == "2026-09" and conds["caliber"] == "tax")
            job = {
                "id": job_id, "page_id": page_id, "task_id": page["task_id"],
                "conditions": conds, "cond_hash": cond_hash(conds),
                "status": "queued", "progress": 0,
                "idempotency_key": idempotency_key, "created_by": actor,
                "created": time.time(), "duration": JOB_MIN_DURATION +
                    (hash(job_id) % int(JOB_MAX_DURATION - JOB_MIN_DURATION + 1)),
                "hang": hang, "nudged": False, "file_id": None, "error": None,
            }
            self.jobs[job_id] = job
            self.idempotency[idempotency_key] = job_id

        # 故障注入：动作已生效，但回执丢失
        if self.chaos.get("lose_receipt"):
            return 500, {"error": "gateway timeout", "code": "RECEIPT_LOST"}
        return 200, {"job_id": job_id, "deduplicated": False, "status": "queued"}

    def nudge(self, token, job_id):
        """人工催办：让卡死的生成继续。"""
        self.check_session(token)
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise PortalError(404, "JOB_NOT_FOUND", "任务不存在")
            job["nudged"] = True
            return dict(job)

    def get_job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    # ---------------- 后台推进（由调度线程周期调用） ----------------

    def tick(self, on_job_done):
        now = time.time()
        done_jobs = []
        with self.lock:
            for job in self.jobs.values():
                if job["status"] == "queued":
                    job["status"] = "running"
                    job["started"] = now
                elif job["status"] == "running":
                    elapsed = now - job.get("started", now)
                    if job["hang"] and not job["nudged"]:
                        # 卡死组合：停在 66%，超时判失败
                        job["progress"] = min(HANG_AT, int(elapsed / 2.0 * 10))
                        if elapsed > HANG_TIMEOUT:
                            job["status"] = "failed"
                            job["error"] = "生成超时（该账期+口径组合在门户侧卡死）"
                    else:
                        job["progress"] = min(100, int(elapsed / job["duration"] * 100))
                        if job["progress"] >= 100:
                            job["status"] = "done"
                            fid = _uid("file")
                            c = job["conditions"]
                            fname = "report_%s_%s_%s_%s.csv" % (
                                c["org"], c["period"], c["caliber"], job["id"][-4:])
                            self.files[fid] = {
                                "id": fid, "job_id": job["id"], "name": fname,
                                "conditions": c, "created": now,
                                "content": self._render_csv(job),
                            }
                            job["file_id"] = fid
                            done_jobs.append(dict(job))
        for job in done_jobs:
            on_job_done(job)     # 回调 core：发通知（可能被 chaos 丢弃）

    @staticmethod
    def _render_csv(job):
        c = job["conditions"]
        lines = [
            "组织,账期,口径,项目,金额",
            "%s,%s,%s,营业收入,1234567.89" % (c["org"], c["period"], c["caliber"]),
            "%s,%s,%s,营业成本,765432.10" % (c["org"], c["period"], c["caliber"]),
            "%s,%s,%s,补充说明,%s" % (c["org"], c["period"], c["caliber"],
                                       (c.get("note") or "-").replace(",", "，")),
        ]
        return "\n".join(lines) + "\n"

    # ---------------- 通知与文件 ----------------

    def notify_download_ready(self, job):
        """生成完成通知。chaos.lose_notify 时通知丢失（动作真实发生，只是没送达）。"""
        delivered = not self.chaos.get("lose_notify")
        with self.lock:
            self.notifications.append({
                "id": _uid("ntf"), "job_id": job["id"], "task_id": job["task_id"],
                "delivered": delivered, "ts": time.time(),
                "message": "报表已生成，可下载（job %s）" % job["id"],
            })
        return delivered

    def list_files(self, task_id=None):
        with self.lock:
            out = []
            for f in self.files.values():
                job = self.jobs.get(f["job_id"], {})
                if task_id is not None and job.get("task_id") != task_id:
                    continue
                out.append(dict(f))
            return out

    def get_file(self, file_id):
        with self.lock:
            f = self.files.get(file_id)
            return dict(f) if f else None

    # ---------------- 视图 ----------------

    def view(self):
        with self.lock:
            now = time.time()
            return {
                "sessions": [
                    {"token": s["token"][-6:], "user": s["user"], "kind": s["kind"],
                     "expires_in": max(0, int(s["expires"] - now))}
                    for s in self.sessions.values()],
                "pages": [dict(p) for p in self.pages.values()],
                "jobs": [dict(j) for j in self.jobs.values()],
                "files": [dict(f) for f in self.files.values()],
                "notifications": list(self.notifications),
            }

    # ---------------- 持久化 ----------------

    def snapshot(self):
        with self.lock:
            return {
                "pages": self.pages, "jobs": self.jobs, "files": self.files,
                "notifications": self.notifications, "idempotency": self.idempotency,
                # 会话不持久化：服务重启即全部过期，走重新登录恢复路径
            }

    def restore(self, data):
        with self.lock:
            self.pages = data.get("pages", {})
            self.jobs = data.get("jobs", {})
            for j in self.jobs.values():   # 重启后未完成任务重新排队
                if j["status"] in ("queued", "running"):
                    j["status"] = "queued"
                    j["progress"] = 0
            self.files = data.get("files", {})
            self.notifications = data.get("notifications", [])
            self.idempotency = data.get("idempotency", {})
