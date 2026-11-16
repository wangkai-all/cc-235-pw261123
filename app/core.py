# -*- coding: utf-8 -*-
"""
采集工作台核心：任务、驱动锁（栅栏令牌）、后台执行器、审计、持久化、对账恢复。

设计决策（对应需求）：
- 保存页面状态还是重放操作？—— 两者都留：门户侧页面状态是唯一事实来源，
  工作台侧保存操作日志（审计 + 幂等键复用）。恢复时重新读取门户真实状态，
  不盲目重放、不沿用缓存。
- 动作已生效但回执丢失 —— 提交使用幂等键，重试去重。
- 交接前后条件与结果归属 —— 每个结果记录条件指纹与产生者；
  交回时以页面当前条件为准更新任务条件，旧条件结果标记「已被取代」。
"""
import json
import os
import threading
import time

from portal import (ACCOUNTS, Portal, PortalError, cond_hash, conds_equal)

WORKBENCH_USERS = {"alice": "alice123", "bob": "bob123"}   # 工作台账号（虚构）
BOT = "bot-01"
BOT_PASSWORD = ACCOUNTS[BOT]["password"]

EXEC_DWELL = 0.9          # 执行器每个动作的间隔（模拟点击耗时）
WAIT_TIMEOUT = 20.0       # 等待生成超过该时长判定卡住
RECONCILE_INTERVAL = 3.0  # 对账周期


class CoreError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


class Core:
    def __init__(self, portal, data_path):
        self.lock = threading.RLock()
        self.portal = portal
        self.data_path = data_path
        self.tasks = {}            # task_id -> task
        self.audit = []            # 全局审计日志
        self.sessions = {}         # 工作台 token -> user
        self.portal_tokens = {}    # user -> 门户会话 token（bot 与各人各一份）
        self.listeners = []        # SSE 订阅者（queue）
        self._last_reconcile = 0.0

    # ================= 通知（SSE） =================

    def subscribe(self, q):
        self.listeners.append(q)

    def unsubscribe(self, q):
        if q in self.listeners:
            self.listeners.remove(q)

    def _broadcast(self):
        for q in list(self.listeners):
            try:
                q.put_nowait("refresh")
            except Exception:
                pass

    # ================= 审计 =================

    def log(self, task_id, actor, action, detail=""):
        entry = {"ts": time.time(), "task_id": task_id,
                 "actor": actor, "action": action, "detail": detail}
        with self.lock:
            self.audit.append(entry)
            self.audit = self.audit[-500:]
        self._persist()
        self._broadcast()

    # ================= 工作台登录 =================

    def login(self, user, password):
        if WORKBENCH_USERS.get(user) != password:
            raise CoreError(401, "BAD_CREDENTIALS", "工作台账号或密码错误")
        token = "wb-" + os.urandom(8).hex()
        with self.lock:
            self.sessions[token] = user
        return token

    def user_of(self, token):
        user = self.sessions.get(token)
        if not user:
            raise CoreError(401, "NOT_LOGGED_IN", "未登录工作台")
        return user

    # ================= 门户会话（按身份各持一份） =================

    def _portal_token(self, user, task_id=None):
        """取该身份的门户会话；过期则重新登录（自动恢复）。"""
        token = self.portal_tokens.get(user)
        if token:
            try:
                self.portal.check_session(token)
                return token, False
            except PortalError:
                pass
        sess = self.portal.login(user, ACCOUNTS[user]["password"])
        self.portal_tokens[user] = sess["token"]
        if task_id:
            self.log(task_id, user, "门户会话恢复",
                     "原会话已过期，已重新登录门户并从中断点继续")
        return sess["token"], True

    def relogin_portal(self, user, task_id=None):
        """人工点击「重新登录门户」。"""
        self.portal_tokens.pop(user, None)
        self._portal_token(user, task_id)
        return True

    # ================= 任务 =================

    def create_task(self, user, name, org, period, caliber):
        task_id = "task-" + os.urandom(4).hex()
        conds = {"org": org, "period": period, "caliber": caliber, "note": ""}
        with self.lock:
            self.tasks[task_id] = {
                "id": task_id, "name": name or ("采集 %s/%s/%s" % (org, period, caliber)),
                "target": dict(conds),          # 目标条件（建单时）
                "current": dict(conds),         # 当前条件（交接中可被调整）
                "status": "queued",             # queued/running/takeover/needs_human/done/failed
                "lock": {"owner": BOT, "token": 1, "since": time.time()},
                "page_id": None, "job_id": None,
                "phase": "init", "phase_since": time.time(),
                "attempt": 0, "results": [],
                "stuck_reason": None, "executor_note": "后台执行器运行中",
                "created": time.time(), "updated": time.time(),
            }
        self.log(task_id, user, "创建任务",
                 "目标条件: %s" % json.dumps(conds, ensure_ascii=False))
        return task_id

    def get_task(self, task_id):
        task = self.tasks.get(task_id)
        if not task:
            raise CoreError(404, "TASK_NOT_FOUND", "任务不存在")
        return task

    # ---------------- 锁（栅栏令牌） ----------------

    def takeover(self, user, task_id, force=False):
        task = self.get_task(task_id)
        with self.lock:
            holder = task["lock"]["owner"]
            if holder and holder != user and holder != BOT and not force:
                raise CoreError(409, "LOCK_HELD",
                                "「%s」正在接管中" % holder,
                                {"holder": holder})
            task["lock"] = {"owner": user, "token": task["lock"]["token"] + 1,
                            "since": time.time()}
            task["status"] = "takeover"
            task["executor_note"] = "后台执行器已暂停（人工接管中）"
            task["updated"] = time.time()
        # 确保该用户在门户侧有会话与绑定页面
        self._portal_token(user, task_id)
        self._ensure_page(task, user)
        how = "强制接管（夺取自 %s）" % holder if (holder and holder != user) else "接管"
        self.log(task_id, user, how, "后台执行器已暂停，页面控制权移交人工")
        return task

    def handback(self, user, task_id):
        task = self.get_task(task_id)
        with self.lock:
            if task["lock"]["owner"] != user:
                raise CoreError(409, "NOT_HOLDER", "当前不持有该任务的控制权")
            # 交回时以门户页面真实状态为准，更新当前条件
            page = self.portal.get_page(task["page_id"]) if task["page_id"] else None
            old = dict(task["current"])
            if page:
                task["current"] = {"org": page["org"], "period": page["period"],
                                   "caliber": page["caliber"],
                                   "note": page.get("note") or ""}
            changed = not conds_equal(old, task["current"])
            # 旧条件结果一律标记「已被取代」，不得沿用
            superseded = 0
            for r in task["results"]:
                if r["status"] == "valid" and not conds_equal(r["conditions"], task["current"]):
                    r["status"] = "superseded"
                    superseded += 1
            # 决定执行器从哪一步继续（已完成的交回不再重启）
            done_already = task["status"] == "done"
            job = self.portal.get_job(task["job_id"]) if task["job_id"] else None
            if done_already:
                pass
            elif job and conds_equal(job["conditions"], task["current"]) \
                    and job["status"] in ("queued", "running"):
                task["phase"] = "wait"          # 已有匹配条件的生成在进行，继续等
            elif job and conds_equal(job["conditions"], task["current"]) \
                    and job["status"] == "done":
                task["phase"] = "download"      # 已生成，直接下载
            else:
                task["phase"] = "submit"        # 条件变了或没有进行中的生成，重新提交
                task["attempt"] += 1
            task["job_id"] = None if task["phase"] == "submit" else task["job_id"]
            task["lock"] = {"owner": BOT, "token": task["lock"]["token"] + 1,
                            "since": time.time()}
            task["status"] = "done" if done_already else "queued"
            task["stuck_reason"] = None
            task["executor_note"] = "已完成" if done_already else "后台执行器运行中"
            task["phase_since"] = time.time()
            task["updated"] = time.time()
        detail = "条件不变，从断点继续" if not changed else \
                 "条件已调整: %s → %s" % (json.dumps(old, ensure_ascii=False),
                                          json.dumps(task["current"], ensure_ascii=False))
        if superseded:
            detail += "；%d 条旧条件结果已标记「已被取代」" % superseded
        self.log(task_id, user, "交回后台", detail)
        return task

    # ---------------- 人工页面操作（需持锁） ----------------

    def _check_holder(self, user, task):
        holder = task["lock"]["owner"]
        if holder != user:
            raise CoreError(409, "STALE_TOKEN",
                            "控制权已转移（当前持有者：%s），你的操作被拒绝" % holder,
                            {"holder": holder})

    def _ensure_page(self, task, user):
        """确保任务有绑定页面。页面绑定任务：游离页面（如人直接上门户
        浏览其他组织）永远不会被当作任务进度。"""
        if task["page_id"] and task["page_id"] in self.portal.pages:
            return self.portal.get_page(task["page_id"])
        token, _ = self._portal_token(user, task["id"])
        page = self.portal.open_page(token, task_id=task["id"])
        with self.lock:
            task["page_id"] = page["id"]
        self.log(task["id"], user, "打开绑定页面",
                 "页面 %s 已绑定到任务（游离页面不计入进度）" % page["id"])
        return page

    def page_action(self, user, task_id, field, value):
        task = self.get_task(task_id)
        with self.lock:
            self._check_holder(user, task)
        self._ensure_page(task, user)
        token, _ = self._portal_token(user, task_id)
        try:
            page = self.portal.set_field(token, task["page_id"], field, value)
        except PortalError as e:
            if e.code == "SESSION_EXPIRED":
                raise CoreError(401, "SESSION_EXPIRED", "门户会话已过期，请重新登录")
            raise CoreError(e.status, e.code, e.message)
        self.log(task_id, user, "页面操作", "设置 %s = %s" % (field, value))
        return page

    def human_submit(self, user, task_id):
        task = self.get_task(task_id)
        with self.lock:
            self._check_holder(user, task)
            attempt = task["attempt"]
        self._ensure_page(task, user)
        job_id, note = self._submit_with_retry(task, user, attempt)
        with self.lock:
            task["attempt"] += 1
            task["job_id"] = job_id
            task["updated"] = time.time()
        self.log(task_id, user, "人工提交生成", "job %s%s" % (job_id, note))
        return job_id

    def nudge(self, user, task_id, job_id):
        task = self.get_task(task_id)
        with self.lock:
            self._check_holder(user, task)
        token, _ = self._portal_token(user, task_id)
        self.portal.nudge(token, job_id)
        self.log(task_id, user, "人工催办", "job %s" % job_id)

    def download(self, user, task_id, job_id):
        task = self.get_task(task_id)
        with self.lock:
            self._check_holder(user, task)
        job = self.portal.get_job(job_id)
        if not job or job["status"] != "done":
            raise CoreError(409, "NOT_READY", "该生成尚未完成")
        self._attach_result(task, job, user, "人工下载")
        with self.lock:
            if conds_equal(job["conditions"], self._reference_conds(task)):
                task["status"] = "done"
                task["executor_note"] = "已完成（人工下载归档）"
        self.log(task_id, user, "下载结果", "文件 %s" % job["file_id"])
        return job["file_id"]

    # ================= 后台执行器 =================

    def tick(self):
        """调度线程周期调用：推进门户、推进执行器、周期对账。"""
        self.portal.tick(self._on_job_done)
        now = time.time()
        with self.lock:
            tasks = list(self.tasks.values())
        for task in tasks:
            try:
                if task["status"] in ("queued", "running") \
                        and task["lock"]["owner"] == BOT:
                    self._executor_step(task, now)
            except Exception as e:      # 执行器自身异常 → 转人工
                self._stuck(task, "执行器异常: %r" % e)
        if now - self._last_reconcile > RECONCILE_INTERVAL:
            self._last_reconcile = now
            for task in tasks:
                if task["status"] not in ("done",):
                    self._reconcile(task)

    def _executor_step(self, task, now):
        if now - task["phase_since"] < EXEC_DWELL:
            return
        token, _ = self._portal_token(BOT, task["id"])
        page = self._ensure_page(task, BOT)
        phase = task["phase"]
        conds = task["current"]

        if phase in ("init", "open_page"):
            self._set_phase(task, "fill_org")
        elif phase == "fill_org":
            self.portal.set_field(token, page["id"], "org", conds["org"])
            self._set_phase(task, "fill_period")
        elif phase == "fill_period":
            self.portal.set_field(token, page["id"], "period", conds["period"])
            self._set_phase(task, "fill_caliber")
        elif phase == "fill_caliber":
            self.portal.set_field(token, page["id"], "caliber", conds["caliber"])
            self._set_phase(task, "submit")
        elif phase == "submit":
            # 控件变化检测：出现执行器不认识的必填控件 → 转人工
            required = self.portal.required_controls(conds)
            missing = [c for c in required
                       if c["field"] == "note" and not (conds.get("note") or "").strip()]
            if missing:
                self._stuck(task, "门户出现新增必填控件「%s」，自动执行器无法处理"
                            % missing[0]["label"])
                return
            job_id, note = self._submit_with_retry(task, BOT, task["attempt"])
            with self.lock:
                task["attempt"] += 1
                task["job_id"] = job_id
            self.log(task["id"], BOT, "后台提交生成", "job %s%s" % (job_id, note))
            self._set_phase(task, "wait")
        elif phase == "wait":
            job = self.portal.get_job(task["job_id"])
            if not job:
                self._stuck(task, "生成任务丢失")
                return
            if job["status"] == "done":
                self._set_phase(task, "download")
            elif job["status"] == "failed":
                self._stuck(task, "生成失败：%s" % (job["error"] or "未知原因"))
            elif now - task["phase_since"] > WAIT_TIMEOUT:
                self._stuck(task, "等待生成超时（%ds 未完成），疑似门户侧卡死" % WAIT_TIMEOUT)
            else:
                with self.lock:     # 继续等，但刷新计时显示进度
                    task["status"] = "running"
        elif phase == "download":
            job = self.portal.get_job(task["job_id"])
            if job and job["status"] == "done":
                self._attach_result(task, job, BOT, "后台下载")
                with self.lock:
                    task["status"] = "done"
                    task["executor_note"] = "已完成"
                self.log(task["id"], BOT, "任务完成",
                         "结果文件 %s 已归档" % job["file_id"])
            else:
                self._stuck(task, "下载时生成任务状态异常")

    def _set_phase(self, task, phase):
        with self.lock:
            task["phase"] = phase
            task["phase_since"] = time.time()
            task["status"] = "running"
            task["updated"] = time.time()

    def _stuck(self, task, reason):
        with self.lock:
            task["status"] = "needs_human"
            task["stuck_reason"] = reason
            task["executor_note"] = "后台执行器已暂停（等待人工接管）"
            task["updated"] = time.time()
        self.log(task["id"], BOT, "执行器卡住", reason)

    def _submit_with_retry(self, task, actor, attempt):
        """提交生成。回执丢失时用同一幂等键重试，门户去重，不会重复建单。"""
        token, _ = self._portal_token(actor, task["id"])
        key = "task:%s:cond:%s:attempt:%d" % (
            task["id"], cond_hash(task["current"]), attempt)
        status, body = self.portal.submit(token, task["page_id"], key, actor)
        if status == 500:
            self.log(task["id"], actor, "提交回执丢失",
                     "动作可能已生效，使用幂等键 %s 重试以确认" % key)
            token, _ = self._portal_token(actor, task["id"])
            status2, body2 = self.portal.submit(token, task["page_id"], key, actor)
            if status2 == 200 and body2.get("deduplicated"):
                self.log(task["id"], actor, "幂等恢复",
                         "门户确认该提交此前已生效（去重），未重复建单")
                return body2["job_id"], "（回执丢失后经幂等键确认）"
            if status2 == 200:
                return body2["job_id"], "（首次回执丢失，重试成功）"
            raise CoreError(502, "SUBMIT_FAILED", "提交失败: %s" % body2)
        return body["job_id"], ""

    # ================= 结果归属与对账 =================

    def _reference_conds(self, task):
        """结果有效性的判定基准：绑定页面填齐条件时以页面实时状态为准
        （页面是唯一事实来源；后台驱动时页面必然等于任务当前条件），
        否则退回任务当前条件。"""
        page = self.portal.pages.get(task["page_id"]) if task["page_id"] else None
        if page and page["org"] and page["period"] and page["caliber"]:
            return {"org": page["org"], "period": page["period"],
                    "caliber": page["caliber"], "note": page.get("note") or ""}
        return task["current"]

    def _attach_result(self, task, job, actor, how):
        with self.lock:
            if any(r["job_id"] == job["id"] for r in task["results"]):
                return
            matches = conds_equal(job["conditions"], self._reference_conds(task))
            if matches:     # 新有效结果出现，旧有效结果让位
                for r in task["results"]:
                    if r["status"] == "valid":
                        r["status"] = "superseded"
            task["results"].append({
                "job_id": job["id"], "file_id": job["file_id"],
                "conditions": dict(job["conditions"]),
                "cond_hash": job["cond_hash"], "by": actor,
                "status": "valid" if matches else "superseded",
                "ts": time.time(),
            })
            task["updated"] = time.time()
        self.log(task["id"], actor, "结果归档（%s）" % how,
                 "job %s，条件指纹 %s，归属 %s，%s" % (
                     job["id"], job["cond_hash"], actor,
                     "与当前条件一致" if matches else "与当前条件不符，标记为已被取代"))

    def _on_job_done(self, job):
        """门户回调：生成完成 → 发通知。通知可能丢失，丢失时靠对账找回。"""
        delivered = self.portal.notify_download_ready(job)
        task = self.tasks.get(job.get("task_id") or "")
        if not task:
            return
        if delivered:
            self.log(task["id"], "portal", "下载完成通知", "job %s 已生成" % job["id"])
        else:
            self.log(task["id"], "portal", "下载完成通知丢失",
                     "job %s 的通知未能送达，等待对账找回" % job["id"])

    def _reconcile(self, task):
        """对账：扫描门户侧属于本任务的文件，补上通知丢失的结果。
        只认绑定页面产生的 job（task_id 匹配），游离页面的文件不会混入。"""
        files = self.portal.list_files(task_id=task["id"])
        for f in files:
            job = self.portal.get_job(f["job_id"])
            if job and not any(r["job_id"] == job["id"] for r in task["results"]):
                self._attach_result(task, job, job["created_by"], "对账找回")
                self.log(task["id"], "core", "对账恢复",
                         "发现未归档结果 job %s（通知可能丢失），已补记" % job["id"])
        # 对账也可能直接完成任务
        with self.lock:
            if task["status"] != "done":
                for r in task["results"]:
                    if r["status"] == "valid" and r["file_id"]:
                        if task["lock"]["owner"] == BOT and task["phase"] in ("wait", "download"):
                            task["status"] = "done"
                            task["executor_note"] = "已完成（对账确认）"
                            self.log(task["id"], "core", "任务完成", "经对账确认结果已就绪")
                        break

    def reconcile_now(self, user, task_id):
        task = self.get_task(task_id)
        self._reconcile(task)
        return True

    # ================= 故障注入 =================

    def chaos(self):
        return self.portal.chaos

    def set_chaos(self, key, value):
        self.portal.chaos[key] = bool(value)
        self.log(None, "chaos", "故障注入", "%s = %s" % (key, value))

    def expire_sessions(self, user):
        n = self.portal.expire_all_sessions()
        self.portal_tokens.clear()
        self.log(None, user, "故障注入", "已使全部门户会话过期（%d 个）" % n)
        return n

    # ================= 视图 / 持久化 =================

    def state(self, user):
        with self.lock:
            tasks = []
            for t in sorted(self.tasks.values(), key=lambda x: x["created"]):
                page = self.portal.pages.get(t["page_id"]) if t["page_id"] else None
                job = self.portal.jobs.get(t["job_id"]) if t["job_id"] else None
                tasks.append({
                    **{k: t[k] for k in ("id", "name", "target", "current", "status",
                                         "lock", "phase", "attempt", "results",
                                         "stuck_reason", "executor_note", "created",
                                         "job_id", "page_id")},
                    "page": page, "job": job,
                    "controls": self.portal.required_controls(
                        page if page else t["current"]),
                    "page_org_mismatch": bool(
                        page and page["org"] and t["current"]["org"]
                        and page["org"] != t["current"]["org"]),
                })
            return {
                "user": user,
                "tasks": tasks,
                "audit": list(reversed(self.audit[-120:])),
                "chaos": dict(self.portal.chaos),
                "portal": self.portal.view(),
                "meta": {"orgs": self._orgs(), "periods": self._periods(),
                         "calibers": self._calibers(),
                         "users": list(WORKBENCH_USERS)},
            }

    @staticmethod
    def _orgs():
        from portal import ORGS
        return ORGS

    @staticmethod
    def _periods():
        from portal import PERIODS
        return PERIODS

    @staticmethod
    def _calibers():
        from portal import CALIBERS
        return CALIBERS

    def open_free_page(self, user):
        """人直接上门户打开的游离页面（不绑定任务）——用于演示
        「另一个组织的页面不会被当作任务进度」。"""
        token, _ = self._portal_token(user)
        page = self.portal.open_page(token, task_id=None)
        self.log(None, user, "打开游离页面", "页面 %s 不绑定任何任务" % page["id"])
        return page

    def free_page_action(self, user, page_id, field, value):
        token, _ = self._portal_token(user)
        return self.portal.set_field(token, page_id, field, value)

    def _persist(self):
        data = {"tasks": self.tasks, "audit": self.audit,
                "chaos": self.portal.chaos, "portal": self.portal.snapshot()}
        tmp = self.data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.data_path)

    def restore(self):
        if not os.path.exists(self.data_path):
            return
        with open(self.data_path, encoding="utf-8") as f:
            data = json.load(f)
        self.tasks = data.get("tasks", {})
        self.audit = data.get("audit", [])
        self.portal.chaos.update(data.get("chaos", {}))
        self.portal.restore(data.get("portal", {}))
        for t in self.tasks.values():
            if t["status"] == "running":      # 重启后执行器从断点继续
                t["status"] = "queued"
                t["phase_since"] = time.time()
        self.log(None, "core", "服务重启", "状态已从磁盘恢复，会话需重新登录")
