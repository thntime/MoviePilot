"""
网盘 API 与应用层补充单元测试。

覆盖个人盘文件用例、非空壳判定、登录与取链编排、个人盘文件接口、账号接口与
网页登录 WebSocket；全部使用内存替身与依赖覆盖，不访问真实网络或数据库。
"""

from __future__ import annotations

import base64
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.adapters.external.netdisk.base import BaseNetdiskAdapter
from app.api.endpoints import netdisk as netdisk_api
from app.api.endpoints import netdisk_files as files_api
from app.application.netdisk import integrity, registry
from app.application.netdisk import resolve as resolve_module
from app.application.netdisk.accounts import NetdiskAccountService, mask_credential
from app.application.netdisk.contract import (
    NetdiskAccountRecord,
    NetdiskAccountView,
    NetdiskAccountWrite,
    NetdiskTuning,
)
from app.application.netdisk.files import NetdiskFilesService
from app.application.netdisk.login import NetdiskLoginService, reset_pending_sessions
from app.application.netdisk.resolve import NetdiskResolveService
from app.domain.netdisk import (
    AccountProbe,
    DirectLink,
    DriveCapabilities,
    DriveFile,
    DriveQuota,
    DriveShare,
    LinkPolicy,
    LoginResult,
    LoginSession,
    NetdiskError,
    NetdiskErrorCode,
    NetdiskPlatform,
    ShareDetail,
    ShareFile,
    TransferHandle,
)


class _Store:
    """
    内存账号仓储替身，实现应用层端口的最小可用子集。
    """

    def __init__(self) -> None:
        """
        创建空仓储。
        """
        self.views: list[NetdiskAccountView] = []
        self.records: dict[int, NetdiskAccountRecord] = {}
        self.updated: list[tuple[int, dict]] = []
        self.deleted: list[int] = []
        self._next = 1

    def list_accounts(self, platform=None):
        """
        按平台过滤脱敏投影。
        """
        if platform is None:
            return list(self.views)
        return [view for view in self.views if view.platform == str(platform)]

    def get_record(self, account_id):
        """
        按主键读取完整记录。
        """
        return self.records.get(account_id)

    def create_account(self, payload: NetdiskAccountWrite):
        """
        写入新账号并返回脱敏投影。
        """
        account_id = self._next
        self._next += 1
        view = NetdiskAccountView(
            id=account_id,
            platform=payload.platform.value,
            name=payload.name,
            is_primary=payload.is_primary,
            status="active",
            masked=mask_credential(payload.credential),
            nickname=payload.nickname,
        )
        self.views.append(view)
        self.records[account_id] = NetdiskAccountRecord(
            id=account_id,
            platform=payload.platform,
            name=payload.name,
            credential=dict(payload.credential),
            is_primary=payload.is_primary,
        )
        return view

    def update_account(
        self,
        account_id,
        *,
        name=None,
        credential=None,
        is_primary=None,
        status=None,
        expires_at=None,
        nickname=None,
        quota_used=None,
        quota_total=None,
    ):
        """
        更新账号并同步脱敏投影。
        """
        self.updated.append(
            (
                account_id,
                {
                    "name": name,
                    "credential": credential,
                    "is_primary": is_primary,
                    "status": status,
                    "nickname": nickname,
                },
            )
        )
        record = self.records.get(account_id)
        index = next((i for i, view in enumerate(self.views) if view.id == account_id), None)
        if record is None or index is None:
            return None
        record = NetdiskAccountRecord(
            id=record.id,
            platform=record.platform,
            name=name or record.name,
            credential=dict(credential or record.credential),
            is_primary=record.is_primary if is_primary is None else is_primary,
            status=status or record.status,
        )
        self.records[account_id] = record
        view = self.views[index]
        updated = NetdiskAccountView(
            id=view.id,
            platform=view.platform,
            name=record.name,
            is_primary=record.is_primary,
            status=record.status,
            expires_at=view.expires_at,
            last_checked_at=view.last_checked_at,
            masked=mask_credential(record.credential),
            nickname=nickname or view.nickname,
            quota_used=view.quota_used if quota_used is None else quota_used,
            quota_total=view.quota_total if quota_total is None else quota_total,
        )
        self.views[index] = updated
        return updated

    def delete_account(self, account_id):
        """
        记录删除请求。
        """
        self.deleted.append(account_id)
        return True

    def touch_account(self, account_id, probe):
        """
        记录探测结果，不做持久化。
        """
        del account_id, probe

    def update_quota(self, account_id, used, total):
        """
        记录空间占用更新。
        """
        self.updated.append((account_id, {"quota_used": used, "quota_total": total}))


class _Settings:
    """
    固定参数快照端口。
    """

    def __init__(self, **kwargs) -> None:
        """
        保存参数快照。
        """
        self.tuning = NetdiskTuning(**kwargs)

    def load(self):
        """
        返回参数快照。
        """
        return self.tuning


class _DriveAdapter(BaseNetdiskAdapter):
    """
    个人盘能力适配器替身。
    """

    platform = NetdiskPlatform.QUARK
    link_policy = LinkPolicy.DIRECT_WITH_FALLBACK
    drive_capabilities = DriveCapabilities(
        quota=True,
        browse=True,
        download=True,
        share=True,
        move=True,
        rename=True,
        delete=True,
    )
    cookie_required_keys = ()
    cookie_domains = (".quark.cn",)
    login_url = "https://pan.quark.cn/"

    def __init__(self, capabilities=None, dirs=None) -> None:
        """
        保存可编排的目录树与能力声明。
        """
        super().__init__()
        if capabilities is not None:
            self.drive_capabilities = capabilities
        self.dirs = dirs or {}
        self.calls: list[tuple] = []

    def quota(self, account):
        """
        返回固定配额。
        """
        return DriveQuota(used=3, total=10)

    def list_dir(self, account, parent_id):
        """
        返回预设目录条目。
        """
        return list(self.dirs.get(parent_id, []))

    def download_file(self, account, file):
        """
        返回固定直链。
        """
        return DirectLink(url="https://cdn.example.com/x", headers={"Cookie": "a=1"}, size=file.size)

    def share_file(self, account, files, *, password="", expires_in=0):
        """
        返回固定分享结果。
        """
        self.calls.append(("share", [item.file_id for item in files], password, expires_in))
        return DriveShare(
            url="https://pan.quark.cn/s/new",
            password=password,
            expires_at=datetime.now() + timedelta(days=1),
        )

    def move_files(self, account, files, target_id):
        """
        记录移动调用。
        """
        self.calls.append(("move", [item.file_id for item in files], target_id))

    def rename_file(self, account, file, name):
        """
        记录重命名调用。
        """
        self.calls.append(("rename", file.file_id, name))

    def delete_files(self, account, files):
        """
        记录删除调用。
        """
        self.calls.append(("delete", [item.file_id for item in files]))


@contextmanager
def _files_env(monkeypatch, adapter=None, with_account=True):
    """
    装配个人盘文件用例所需的注册表与账号。
    """
    store = _Store()
    adapter = adapter or _DriveAdapter()
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: adapter})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_Settings(),
        cookie_import=None,
        login_adapter=None,
    )
    account = None
    if with_account:
        account = store.create_account(
            NetdiskAccountWrite(
                platform=NetdiskPlatform.QUARK,
                name="主账号",
                credential={"cookie": "a=1"},
                is_primary=True,
            )
        )
    try:
        yield NetdiskFilesService(), adapter, store, account
    finally:
        registry.reset_netdisk_services()


def test_files_capabilities_and_quota(monkeypatch) -> None:
    """
    能力声明与空间配额透传平台适配器。
    """
    with _files_env(monkeypatch) as (service, adapter, _store, account):
        caps = service.capabilities(account.id)
        quota = service.quota(account.id)

    assert caps.download is True
    assert quota == DriveQuota(used=3, total=10)
    assert adapter.drive_capabilities.browse is True


def test_files_missing_account_raises(monkeypatch) -> None:
    """
    账号不存在时统一抛出 UNSUPPORTED。
    """
    with _files_env(monkeypatch) as (service, _adapter, _store, _account):
        with pytest.raises(NetdiskError) as error:
            service.capabilities(99)

    assert error.value.code is NetdiskErrorCode.UNSUPPORTED
    assert error.value.message == "网盘账号不存在"


def test_files_rejects_unsupported_capabilities(monkeypatch) -> None:
    """
    平台未声明能力时逐项拒绝，不调用适配器。
    """
    adapter = _DriveAdapter(capabilities=DriveCapabilities())
    file = DriveFile(file_id="f1", name="a.mkv", size=1)
    with _files_env(monkeypatch, adapter=adapter) as (service, _adapter, _store, account):
        for call in (
            lambda: service.quota(account.id),
            lambda: service.list_dir(account.id),
            lambda: service.download(account.id, file),
            lambda: service.walk(account.id),
            lambda: service.share(account.id, [file]),
            lambda: service.move(account.id, [file], "t"),
            lambda: service.rename(account.id, file, "b.mkv"),
            lambda: service.delete(account.id, [file]),
        ):
            with pytest.raises(NetdiskError) as error:
                call()
            assert error.value.code is NetdiskErrorCode.UNSUPPORTED

    assert adapter.calls == []


def test_files_list_dir_and_download(monkeypatch) -> None:
    """
    目录浏览与取直链透传父目录与文件条目。
    """
    adapter = _DriveAdapter(dirs={"": [DriveFile(file_id="f1", name="a.mkv", size=4)]})
    with _files_env(monkeypatch, adapter=adapter) as (service, _adapter, _store, account):
        files = service.list_dir(account.id, "")
        link = service.download(account.id, files[0])

    assert [item.file_id for item in files] == ["f1"]
    assert link.url == "https://cdn.example.com/x"
    assert link.size == 4


def test_files_walk_recurses_and_limits_depth(monkeypatch) -> None:
    """
    递归展开目录并遵守最大深度，结果只保留文件。
    """
    adapter = _DriveAdapter(
        dirs={
            "": [
                DriveFile(file_id="d1", name="S1", is_dir=True),
                DriveFile(file_id="f1", name="a.mkv", size=1),
            ],
            "d1": [DriveFile(file_id="f2", name="b.mkv", size=2)],
        }
    )
    with _files_env(monkeypatch, adapter=adapter) as (service, _adapter, _store, account):
        walked = service.walk(account.id)
        shallow = service.walk(account.id, max_depth=0)

    assert sorted(item.file_id for item in walked) == ["f1", "f2"]
    assert [item.file_id for item in shallow] == ["f1"]


def test_files_share_move_rename_delete(monkeypatch) -> None:
    """
    分享、移动、重命名与删除按参数转发适配器。
    """
    file = DriveFile(file_id="f1", name="a.mkv", size=1)
    with _files_env(monkeypatch) as (service, adapter, _store, account):
        share = service.share(account.id, [file], password="abcd", expires_in=3600)
        service.move(account.id, [file], "target")
        service.rename(account.id, file, "b.mkv")
        service.delete(account.id, [file])

    assert share.url == "https://pan.quark.cn/s/new"
    assert share.password == "abcd"
    assert adapter.calls == [
        ("share", ["f1"], "abcd", 3600),
        ("move", ["f1"], "target"),
        ("rename", "f1", "b.mkv"),
        ("delete", ["f1"]),
    ]


def test_integrity_probe_reads_duration(monkeypatch, tmp_path: Path) -> None:
    """
    ffprobe 成功时返回浮点时长。
    """
    monkeypatch.setattr(
        integrity.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="12.5\n"),
    )

    assert integrity.probe_video_duration(tmp_path / "a.mkv") == 12.5


def test_integrity_probe_failure_paths(monkeypatch, tmp_path: Path) -> None:
    """
    非零返回、不可解析输出与执行异常都退化为 None。
    """
    target = tmp_path / "a.mkv"

    monkeypatch.setattr(
        integrity.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="")
    )
    assert integrity.probe_video_duration(target) is None

    monkeypatch.setattr(
        integrity.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="abc")
    )
    assert integrity.probe_video_duration(target) is None

    def _timeout(*args, **kwargs):
        """模拟 ffprobe 超时。"""
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)

    monkeypatch.setattr(integrity.subprocess, "run", _timeout)
    assert integrity.probe_video_duration(target) is None

    def _missing(*args, **kwargs):
        """模拟 ffprobe 不存在。"""
        raise OSError("missing")

    monkeypatch.setattr(integrity.subprocess, "run", _missing)
    assert integrity.probe_video_duration(target) is None


def test_integrity_stat_error_is_incomplete(monkeypatch, tmp_path: Path) -> None:
    """
    读取文件状态抛出 OSError 时按不完整处理。
    """

    def _broken(self):
        """模拟无法访问文件系统。"""
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "is_file", _broken)

    assert integrity.is_complete_file(tmp_path / "a.mkv") is False


def test_integrity_empty_and_plan_files_are_incomplete(tmp_path: Path) -> None:
    """
    零字节文件与残留计划文件都判为未完成。
    """
    empty = tmp_path / "empty.mkv"
    empty.touch()
    assert integrity.is_complete_file(empty) is False

    pending = tmp_path / "video.mkv"
    pending.write_bytes(b"x")
    pending.with_suffix(".mkv.plan.json").write_text("{}", encoding="utf-8")
    assert integrity.is_complete_file(pending) is False

    marked = tmp_path / "done.mkv"
    marked.write_bytes(b"x")
    marked.with_name(marked.name + ".done").touch()
    assert integrity.is_complete_file(marked) is True


class _LoginPlatformAdapter(BaseNetdiskAdapter):
    """
    登录用例的平台适配器替身。
    """

    platform = NetdiskPlatform.PAN123
    link_policy = LinkPolicy.DIRECT
    cookie_required_keys = ("token",)
    cookie_domains = (".123pan.com",)
    login_url = "https://www.123pan.com/"
    supports_password_login = True

    def __init__(self) -> None:
        """
        创建可编排的登录适配器。
        """
        super().__init__()
        self.sms_result = {"verification_id": "v-1", "is_user": True, "expires_in": 300}
        self.sms_credential = {"token": "jwt-sms"}
        self.probe = AccountProbe(status="active")
        self.ready = True
        self.account_name = "测试账号"

    def validate_credential(self, credential):
        """
        返回预设的校验结果。
        """
        return self.probe

    def password_login(self, username, password):
        """
        密码正确时返回令牌。
        """
        if password != "ok":
            raise NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "账号或密码错误", platform=self.platform)
        return {"token": f"jwt-{username}"}

    def login_ready(self, credential):
        """
        返回预设的就绪状态。
        """
        del credential
        return self.ready

    def login_account_name(self, credential):
        """
        返回预设昵称。
        """
        del credential
        return self.account_name

    def send_sms_code(self, phone):
        """
        返回预设发码结果。
        """
        return dict(self.sms_result, phone=phone)

    def sms_login(self, phone, code, verification_id):
        """
        返回预设短信凭据。
        """
        del phone, code, verification_id
        return dict(self.sms_credential)


class _NoSmsAdapter(_LoginPlatformAdapter):
    """
    不支持短信登录的平台适配器替身。
    """

    send_sms_code = None
    sms_login = None


class _LoginAdapter:
    """
    网页登录端口替身。
    """

    def __init__(self) -> None:
        """
        创建可编排的登录端口。
        """
        self.started: list[tuple] = []
        self.cancelled: list[str] = []
        self.focus_calls: list[str] = []
        self.inputs: list[tuple] = []
        self.streams: list[tuple] = []
        self.stopped: list[str] = []
        self.events: list[tuple] = []
        self.result = LoginResult(status="success", credential={"token": "jwt-qr"})
        self.frame = self._frame
        self.input = self._input
        self.start_stream = self._start_stream
        self.stop_stream = self._stop_stream
        self.send_event = self._send_event
        self.focus_error = False

    def start(self, platform, login_url, account_id=None):
        """
        返回固定登录会话。
        """
        self.started.append((platform, login_url, account_id))
        return LoginSession(
            session_id="s-1",
            platform=platform,
            image=b"qr-png",
            page_image=b"page-png",
            status="pending",
            expires_at=datetime.now() + timedelta(minutes=5),
        )

    def poll(self, session_id):
        """
        返回预设轮询结果。
        """
        del session_id
        return self.result

    def focus_main_page(self, session_id):
        """
        记录切回主页面调用，可配置为失败。
        """
        if self.focus_error:
            raise RuntimeError("focus failed")
        self.focus_calls.append(session_id)

    def cancel(self, session_id):
        """
        记录取消调用。
        """
        self.cancelled.append(session_id)

    def _frame(self, session_id):
        """返回固定画面。"""
        del session_id
        return {"image": b"img", "width": 10, "height": 20}

    def _input(self, session_id, **kwargs):
        """记录页面操作。"""
        self.inputs.append((session_id, kwargs))

    def _start_stream(self, session_id, on_frame):
        """记录实时画面启动。"""
        self.streams.append((session_id, on_frame))

    def _stop_stream(self, session_id):
        """记录实时画面停止。"""
        self.stopped.append(session_id)

    def _send_event(self, session_id, event):
        """记录页面事件。"""
        self.events.append((session_id, event))


class _CookieImport:
    """
    CookieCloud 导入端口替身。
    """

    def __init__(self, cookies) -> None:
        """
        保存待返回的 Cookie 映射。
        """
        self.cookies = cookies

    def import_cookies(self, domains):
        """
        返回预设 Cookie。
        """
        del domains
        return self.cookies


@contextmanager
def _login_env(monkeypatch, adapter=None, login_adapter=None, cookies=None):
    """
    装配登录用例所需的注册表与服务。
    """
    store = _Store()
    adapter = adapter or _LoginPlatformAdapter()
    login_adapter = login_adapter or _LoginAdapter()
    cookie_import = _CookieImport(cookies if cookies is not None else {"token": "jwt-cc"})
    registry.configure_netdisk_adapters({NetdiskPlatform.PAN123: adapter})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_Settings(),
        cookie_import=cookie_import,
        login_adapter=login_adapter,
    )
    service = NetdiskLoginService(NetdiskAccountService(store), pending={})
    try:
        yield service, store, adapter, login_adapter, cookie_import
    finally:
        reset_pending_sessions()
        registry.reset_netdisk_services()


def test_login_paste_cookie_rejects_empty_and_updates(monkeypatch) -> None:
    """
    空文本被拒绝；指定账号时更新已有凭据而不新建。
    """
    with _login_env(monkeypatch) as (service, store, _adapter, _login, _cookies):
        with pytest.raises(NetdiskError) as error:
            service.paste_cookie("pan123", "   ")
        assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED

        created = service.paste_cookie("pan123", "jwt-first", name="主账号")
        updated = service.paste_cookie("pan123", "jwt-second", account_id=created.id)

    assert updated.id == created.id
    assert store.records[created.id].credential == {"token": "jwt-second"}
    assert len(store.views) == 1


def test_login_cookiecloud_rejects_empty_values(monkeypatch) -> None:
    """
    CookieCloud 返回的字段值全为空白时视为空 Cookie。
    """
    with _login_env(monkeypatch, cookies={"token": "   "}) as (service, _store, _adapter, _login, _import):
        with pytest.raises(NetdiskError) as error:
            service.import_cookiecloud("pan123")

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED
    assert "Cookie 为空" in error.value.message


def test_login_send_sms_code_paths(monkeypatch) -> None:
    """
    短信发码透传适配器；平台未实现时返回 UNSUPPORTED。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, _login, _import):
        result = service.send_sms_code("pan123", " 13800000000 ")
    assert result["verification_id"] == "v-1"
    assert result["phone"] == "13800000000"

    with _login_env(monkeypatch, adapter=_NoSmsAdapter()) as (service, _store, _adapter, _login, _import):
        with pytest.raises(NetdiskError) as error:
            service.send_sms_code("pan123", "13800000000")
    assert error.value.code is NetdiskErrorCode.UNSUPPORTED


def test_login_sms_login_paths(monkeypatch) -> None:
    """
    短信登录覆盖未支持、空凭据、更新已有账号与新建账号。
    """
    with _login_env(monkeypatch, adapter=_NoSmsAdapter()) as (service, _store, _adapter, _login, _import):
        with pytest.raises(NetdiskError) as error:
            service.sms_login("pan123", "138", "1234", "v-1")
        assert error.value.code is NetdiskErrorCode.UNSUPPORTED

    with _login_env(monkeypatch) as (service, store, adapter, _login, _import):
        adapter.sms_credential = {}
        with pytest.raises(NetdiskError) as error:
            service.sms_login("pan123", "138", "1234", "v-1")
        assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED

        adapter.sms_credential = {"token": "jwt-sms"}
        created = service.sms_login("pan123", "138", "1234", "v-1", name="主账号")
        updated = service.sms_login("pan123", "139", "5678", "v-2", account_id=created.id)

    assert updated.id == created.id
    assert store.records[created.id].credential == {"token": "jwt-sms"}


def test_login_start_qr_prunes_expired_sessions(monkeypatch) -> None:
    """
    启动新会话时清理已过期的待完成上下文。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        service.start_qr("pan123")
        service._pending["stale"] = service._pending["s-1"]  # pylint: disable=protected-access
        service._pending["stale"].expires_at = time.monotonic() - 1  # pylint: disable=protected-access
        session = service.start_qr("pan123")

    assert session.session_id == "s-1"
    assert "stale" not in service._pending  # pylint: disable=protected-access
    assert login_adapter.started


def test_login_poll_qr_missing_and_expired(monkeypatch) -> None:
    """
    会话不存在或已过期时返回失败。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, _login, _import):
        missing = service.poll_qr("unknown")
        service.start_qr("pan123")
        service._pending["s-1"].expires_at = time.monotonic() - 1  # pylint: disable=protected-access
        expired = service.poll_qr("s-1")

    assert missing.status == "failed"
    assert expired.status == "failed"
    assert "不存在或已过期" in expired.message


def test_login_poll_qr_returns_detected_without_repolling(monkeypatch) -> None:
    """
    已检测到登录态时直接返回 detected，不再调用平台接口。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        service.start_qr("pan123")
        context = service._pending["s-1"]  # pylint: disable=protected-access
        context.credential = {"token": "jwt"}
        context.account_name = "昵称"
        login_adapter.poll = lambda session_id: pytest.fail("不应重复轮询")
        result = service.poll_qr("s-1")

    assert result.status == "detected"
    assert result.account_name == "昵称"


def test_login_poll_qr_success_marks_detected(monkeypatch) -> None:
    """
    平台确认登录后保存上下文、切回主页面并返回 detected。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        service.start_qr("pan123")
        result = service.poll_qr("s-1")

    assert result.status == "detected"
    assert result.account_name == "测试账号"
    assert login_adapter.focus_calls == ["s-1"]


def test_login_poll_qr_focus_failure_is_swallowed(monkeypatch) -> None:
    """
    切回主页面失败不影响已检测到的登录态。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        login_adapter.focus_error = True
        service.start_qr("pan123")
        result = service.poll_qr("s-1")

    assert result.status == "detected"


def test_login_poll_qr_waits_for_readiness(monkeypatch) -> None:
    """
    平台未就绪时保持 pending，不校验凭据。
    """
    with _login_env(monkeypatch) as (service, _store, adapter, _login, _import):
        adapter.ready = False
        service.start_qr("pan123")
        result = service.poll_qr("s-1")

    assert result.status == "pending"
    assert result.message == "正在等待登录完成"


def test_login_poll_qr_missing_fields_stays_pending(monkeypatch) -> None:
    """
    凭据缺少必填字段时继续等待，不累计失败次数。
    """
    with _login_env(monkeypatch) as (service, _store, adapter, _login, _import):
        adapter.probe = AccountProbe(status="expired", message="凭据缺少必填字段：token")
        service.start_qr("pan123")
        result = service.poll_qr("s-1")
        context = service._pending["s-1"]  # pylint: disable=protected-access

    assert result.status == "pending"
    assert context.validation_failures == 0


def test_login_poll_qr_tolerates_validation_failures_then_fails(monkeypatch) -> None:
    """
    校验失败先宽限 30 次，超过后结束会话并返回失败。
    """
    with _login_env(monkeypatch) as (service, _store, adapter, _login, _import):
        adapter.probe = AccountProbe(status="expired", message="令牌无效")
        service.start_qr("pan123")
        pending = [service.poll_qr("s-1") for _ in range(29)]
        failed = service.poll_qr("s-1")
        gone = service.poll_qr("s-1")

    assert pending[-1].status == "pending"
    assert failed.status == "failed"
    assert failed.message == "令牌无效"
    assert gone.status == "failed"
    assert "不存在或已过期" in gone.message


def test_login_poll_qr_expired_result_drops_session(monkeypatch) -> None:
    """
    平台返回 expired 时移除待完成会话。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        login_adapter.result = LoginResult(status="expired", message="已过期")
        service.start_qr("pan123")
        result = service.poll_qr("s-1")

    assert result.status == "expired"
    assert "s-1" not in service._pending  # pylint: disable=protected-access


def test_login_confirm_qr_requires_detected_credential(monkeypatch) -> None:
    """
    没有待确认登录态时确认操作失败。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, _login, _import):
        with pytest.raises(NetdiskError) as error:
            service.confirm_qr("unknown")

    assert error.value.code is NetdiskErrorCode.AUTH_EXPIRED


def test_login_confirm_qr_creates_and_updates(monkeypatch) -> None:
    """
    确认登录态时新建账号，指定账号时更新已有账号。
    """
    with _login_env(monkeypatch) as (service, store, _adapter, login_adapter, _import):
        service.start_qr("pan123", name="主账号")
        service.poll_qr("s-1")
        created = service.confirm_qr("s-1")
        assert login_adapter.cancelled == ["s-1"]

        service.start_qr("pan123", account_id=created.id)
        service.poll_qr("s-1")
        updated = service.confirm_qr("s-1")

    assert updated.id == created.id
    assert store.records[created.id].credential == {"token": "jwt-qr"}
    assert len(store.views) == 1


def test_login_cancel_qr_removes_session(monkeypatch) -> None:
    """
    取消会话时移除上下文并释放浏览器。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        service.start_qr("pan123")
        service.cancel_qr("s-1")

    assert "s-1" not in service._pending  # pylint: disable=protected-access
    assert login_adapter.cancelled == ["s-1"]


def test_login_qr_frame_and_input(monkeypatch) -> None:
    """
    画面读取与页面操作转发登录端口，未实现时返回 UNSUPPORTED。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        frame = service.qr_frame("s-1")
        service.qr_input("s-1", kind="click", x=1, y=2)

        login_adapter.frame = None
        with pytest.raises(NetdiskError) as error:
            service.qr_frame("s-1")
        assert error.value.code is NetdiskErrorCode.UNSUPPORTED

        login_adapter.input = None
        with pytest.raises(NetdiskError) as error:
            service.qr_input("s-1", kind="click")
        assert error.value.code is NetdiskErrorCode.UNSUPPORTED

    assert frame == {"image": b"img", "width": 10, "height": 20}
    assert login_adapter.inputs == [("s-1", {"kind": "click", "x": 1, "y": 2})]


def test_login_qr_stream_and_events(monkeypatch) -> None:
    """
    实时画面启停与页面事件转发，未实现时返回 UNSUPPORTED。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, login_adapter, _import):
        service.qr_stream_start("s-1", lambda *args: None)
        service.qr_send_event("s-1", {"kind": "key"})
        service.qr_stream_stop("s-1")

        login_adapter.start_stream = None
        with pytest.raises(NetdiskError) as error:
            service.qr_stream_start("s-1", lambda *args: None)
        assert error.value.code is NetdiskErrorCode.UNSUPPORTED

        login_adapter.send_event = None
        with pytest.raises(NetdiskError) as error:
            service.qr_send_event("s-1", {})
        assert error.value.code is NetdiskErrorCode.UNSUPPORTED

        login_adapter.stop_stream = None
        service.qr_stream_stop("s-1")

    assert len(login_adapter.streams) == 1
    assert login_adapter.events == [("s-1", {"kind": "key"})]
    assert login_adapter.stopped == ["s-1"]


def test_login_credential_from_text_variants(monkeypatch) -> None:
    """
    裸 JWT 识别为 token，Cookie 头识别为 cookie。
    """
    with _login_env(monkeypatch) as (service, _store, _adapter, _login, _import):
        assert service.credential_from_text(NetdiskPlatform.PAN123, "jwt-value") == {"token": "jwt-value"}
        assert service.credential_from_text(NetdiskPlatform.GUANGYA, "jwt-value") == {"token": "jwt-value"}
        assert service.credential_from_text(NetdiskPlatform.PAN115, "no-equals") == {"token": "no-equals"}
        assert service.credential_from_text(NetdiskPlatform.PAN115, "a=1; b=2") == {"cookie": "a=1; b=2"}


def test_login_target_rejects_missing_and_mismatched_account(monkeypatch) -> None:
    """
    指定账号不存在或与平台不一致时抛出错误。
    """
    with _login_env(monkeypatch) as (service, store, _adapter, _login, _import):
        created = store.create_account(
            NetdiskAccountWrite(platform=NetdiskPlatform.PAN123, name="主账号", credential={"token": "t"})
        )
        with pytest.raises(NetdiskError) as missing:
            service.paste_cookie("pan123", "jwt", account_id=99)
        with pytest.raises(NetdiskError) as mismatch:
            service.paste_cookie("quark", "jwt", account_id=created.id)
        with pytest.raises(NetdiskError) as unknown:
            service.paste_cookie(None, "jwt")

    assert missing.value.message == "账号不存在"
    assert mismatch.value.message == "账号与平台不匹配"
    assert unknown.value.code is NetdiskErrorCode.UNSUPPORTED


class _ResolveAdapter(BaseNetdiskAdapter):
    """
    取链编排测试用的平台适配器替身。
    """

    platform = NetdiskPlatform.QUARK
    link_policy = LinkPolicy.DIRECT_WITH_FALLBACK
    drive_capabilities = DriveCapabilities(quota=True, browse=True, delete=True)

    def __init__(self) -> None:
        """
        创建可编排的取链适配器。
        """
        super().__init__()
        self.resolve_errors: list[Exception] = []
        self.cleanup_error: Exception | None = None
        self.quota_error: Exception | None = None
        self.quota_value = DriveQuota(used=0, total=100)
        self.dir_entries: dict[int, object] = {}
        self.delete_error: Exception | None = None
        self.deleted: list[list[str]] = []

    def resolve(self, ref, file, account, *, allow_transfer=True, transfer_first=False):
        """
        按预设错误序列抛错，否则返回直链。
        """
        del ref, allow_transfer, transfer_first
        if self.resolve_errors:
            raise self.resolve_errors.pop(0)
        return DirectLink(
            url=f"https://cdn.example.com/{file.file_id}",
            headers={},
            size=file.size,
            account_id=account.id if account else 0,
        )

    def cleanup(self, handle, account):
        """
        按需抛错，记录清理调用。
        """
        del handle, account
        if self.cleanup_error is not None:
            raise self.cleanup_error

    def quota(self, account):
        """
        按需抛错，返回预设配额。
        """
        del account
        if self.quota_error is not None:
            raise self.quota_error
        return self.quota_value

    def list_dir(self, account, parent_id):
        """
        返回预设目录或抛出预设异常。
        """
        del parent_id
        value = self.dir_entries.get(account.id, [])
        if isinstance(value, Exception):
            raise value
        return list(value)

    def delete_files(self, account, files):
        """
        按需抛错，记录删除的目录。
        """
        del account
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append([item.file_id for item in files])


class _ResolveAccounts:
    """
    取链编排使用的账号用例替身。
    """

    def __init__(self, store: _Store, views=None) -> None:
        """
        保存仓储、视图与可编排的错误。
        """
        self.store = store
        self.views = list(views or [])
        self.select_result = None
        self.select_error: Exception | None = None
        self.list_error: Exception | None = None
        self.refresh_error: Exception | None = None

    def select(self, platform, exclude_ids=(), required_bytes=0):
        """
        返回预设账号或抛出预设异常。
        """
        del platform, exclude_ids, required_bytes
        if self.select_error is not None:
            raise self.select_error
        return self.select_result

    def require_account(self, platform, account_id):
        """
        按主键读取账号。
        """
        del platform
        return self.store.get_record(account_id)

    def list(self, platform=None):
        """
        返回预设视图或抛出预设异常。
        """
        del platform
        if self.list_error is not None:
            raise self.list_error
        return self.views

    def refresh(self, account_id):
        """
        返回仓储记录或抛出预设异常。
        """
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.store.get_record(account_id)


def _record(account_id: int = 1) -> NetdiskAccountRecord:
    """
    构造账号记录。
    """
    return NetdiskAccountRecord(
        id=account_id,
        platform=NetdiskPlatform.QUARK,
        name=f"账号{account_id}",
        credential={"cookie": "a=1"},
    )


@contextmanager
def _resolve_env(monkeypatch, adapter=None):
    """
    装配取链服务、适配器与账号替身。
    """
    monkeypatch.setattr(resolve_module, "_PLATFORM_MIN_INTERVAL", 0)
    store = _Store()
    store.records[1] = _record(1)
    store.records[2] = _record(2)
    adapter = adapter or _ResolveAdapter()
    accounts = _ResolveAccounts(store, views=[])
    registry.configure_netdisk_adapters({NetdiskPlatform.QUARK: adapter})
    registry.configure_netdisk_services(
        account_store=store,
        settings=_Settings(),
        cookie_import=None,
        login_adapter=None,
    )
    service = NetdiskResolveService(accounts=accounts, settings=_Settings())
    try:
        yield service, adapter, accounts, store
    finally:
        registry.reset_netdisk_services()


def test_resolve_cleanup_without_handle(monkeypatch) -> None:
    """
    没有清理句柄时视为无需清理。
    """
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        assert service.cleanup(DirectLink(url="x", headers={})) is True


def test_resolve_cleanup_failures_return_false(monkeypatch) -> None:
    """
    分类异常与未分类异常都不阻断下载主流程，只返回 False。
    """
    handle = TransferHandle(platform=NetdiskPlatform.QUARK, account_id=1, directory_id="d1")
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.cleanup_error = NetdiskError(NetdiskErrorCode.UNKNOWN, "清理失败")
        classified = service.cleanup(DirectLink(url="x", headers={}, cleanup_handle=handle))

        adapter.cleanup_error = RuntimeError("boom")
        unexpected = service.cleanup(DirectLink(url="x", headers={}, cleanup_handle=handle))

    assert classified is False
    assert unexpected is False


def test_resolve_cleanup_success_clears_quota_cache(monkeypatch) -> None:
    """
    清理成功后失效该账号的配额缓存。
    """
    handle = TransferHandle(platform=NetdiskPlatform.QUARK, account_id=1, directory_id="d1")
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        service._quota_cache[1] = (0.0, 10, 20)  # pylint: disable=protected-access
        assert service.cleanup(DirectLink(url="x", headers={}, cleanup_handle=handle)) is True
        assert 1 not in service._quota_cache  # pylint: disable=protected-access


def test_resolve_force_refresh_refreshes_account(monkeypatch) -> None:
    """
    强制刷新时先续期账号再取链。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        account = _record(1)
        accounts.select_result = account
        link = service._resolve_one(  # pylint: disable=protected-access
            adapter,
            resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1"),
            file,
            account,
            force_refresh=True,
        )

    assert link.url.endswith("/f1")


def test_resolve_space_insufficient_recovers(monkeypatch) -> None:
    """
    取链时空间不足先冷却账号并清理临时目录，再重试成功。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        account = _record(1)
        accounts.select_result = None
        adapter.resolve_errors = [
            NetdiskError(NetdiskErrorCode.SPACE_INSUFFICIENT, "空间不足", platform=NetdiskPlatform.QUARK)
        ]
        adapter.dir_entries = {1: [DriveFile(file_id="tmp", name="mp_tr_old", is_dir=True)]}
        link = service._resolve_one(  # pylint: disable=protected-access
            adapter,
            resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1"),
            file,
            account,
            force_refresh=False,
        )

    assert link.url.endswith("/f1")
    assert adapter.deleted == [["tmp"]]


def test_resolve_required_bytes_zero_for_unknown_size(monkeypatch) -> None:
    """
    文件大小未知时峰值需求为 0。
    """
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        assert service._required_bytes(ShareFile(file_id="f", name="a", size=0), 3) == 0  # pylint: disable=protected-access
        assert service._required_bytes(ShareFile(file_id="f", name="a", size=10), 0) == 12  # pylint: disable=protected-access


def test_resolve_select_transfer_target_reraises_other_errors(monkeypatch) -> None:
    """
    非空间不足错误直接向上抛出。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        def _raise(*args, **kwargs):
            """模拟限流错误。"""
            del args, kwargs
            raise NetdiskError(NetdiskErrorCode.RATE_LIMITED, "太频繁", platform=NetdiskPlatform.QUARK)

        monkeypatch.setattr(service, "_ensure_space", _raise)
        with pytest.raises(NetdiskError) as error:
            service._select_transfer_target(  # pylint: disable=protected-access
                adapter, ShareFile(file_id="f", name="a.mkv", size=10), _record(1), 10, 1,
            )

    assert error.value.code is NetdiskErrorCode.RATE_LIMITED


def test_resolve_select_transfer_target_switches_account(monkeypatch) -> None:
    """
    空间不足时切换到放得下的其他账号。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        calls: list[int] = []

        def _ensure(_adapter, _file, account, *, concurrency=1):
            """首次空间不足，第二次通过。"""
            del _adapter, _file, concurrency
            calls.append(account.id)
            if len(calls) == 1:
                raise NetdiskError(
                    NetdiskErrorCode.SPACE_INSUFFICIENT, "空间不足", platform=NetdiskPlatform.QUARK
                )

        monkeypatch.setattr(service, "_ensure_space", _ensure)
        accounts.select_result = _record(2)
        current, allow, strategy = service._select_transfer_target(  # pylint: disable=protected-access
            adapter, ShareFile(file_id="f", name="a.mkv", size=10), _record(1), 10, 1,
        )

    assert current.id == 2
    assert allow is True
    assert strategy == "transfer"


def test_resolve_recover_runtime_capacity_prefers_other_account(monkeypatch) -> None:
    """
    运行时容量不足时优先换到其他账号。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.select_result = _record(2)
        recovered = service._recover_runtime_capacity(adapter, _record(1), 10)  # pylint: disable=protected-access

    assert recovered.id == 2


def test_resolve_can_ever_fit_variants(monkeypatch) -> None:
    """
    无需求或配额未知时认为可能放得下。
    """
    view = NetdiskAccountView(
        id=1, platform="quark", name="账号", is_primary=True, status="active", quota_total=0
    )
    with _resolve_env(monkeypatch) as (service, _adapter, accounts, _store):
        assert service._can_ever_fit(NetdiskPlatform.QUARK, 0) is True  # pylint: disable=protected-access

        accounts.list_error = NetdiskError(NetdiskErrorCode.UNKNOWN, "boom")
        assert service._can_ever_fit(NetdiskPlatform.QUARK, 10) is True  # pylint: disable=protected-access

        accounts.list_error = None
        accounts.views = [view]
        assert service._can_ever_fit(NetdiskPlatform.QUARK, 10) is True  # pylint: disable=protected-access


def test_resolve_purge_skips_without_capabilities(monkeypatch) -> None:
    """
    缺少浏览或删除能力时不清理临时目录。
    """
    adapter = _ResolveAdapter()
    adapter.drive_capabilities = DriveCapabilities()
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        assert service._purge_temp_dirs(adapter, NetdiskPlatform.QUARK) == 0  # pylint: disable=protected-access


def test_resolve_purge_skips_failed_accounts(monkeypatch) -> None:
    """
    账号列表或目录读取失败时跳过对应账号，不阻断清理。
    """
    view = NetdiskAccountView(id=1, platform="quark", name="账号", is_primary=True, status="active")
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.list_error = NetdiskError(NetdiskErrorCode.UNKNOWN, "boom")
        adapter.dir_entries = {1: RuntimeError("list failed")}
        assert service._purge_temp_dirs(  # pylint: disable=protected-access
            adapter, NetdiskPlatform.QUARK, _record(1)
        ) == 0

        accounts.list_error = None
        accounts.views = [view]
        assert service._purge_temp_dirs(adapter, NetdiskPlatform.QUARK) == 0  # pylint: disable=protected-access


def test_resolve_purge_skips_delete_failure(monkeypatch) -> None:
    """
    删除临时目录失败时记录告警并继续。
    """
    view = NetdiskAccountView(id=1, platform="quark", name="账号", is_primary=True, status="active")
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.views = [view]
        adapter.dir_entries = {1: [DriveFile(file_id="tmp", name="MoviePilot临时转存", is_dir=True)]}
        adapter.delete_error = RuntimeError("delete failed")
        removed = service._purge_temp_dirs(adapter, NetdiskPlatform.QUARK)  # pylint: disable=protected-access

    assert removed == 0


def test_resolve_purge_skips_excluded_directories(monkeypatch) -> None:
    """
    exclude 返回的目录不被删除。
    """
    view = NetdiskAccountView(id=1, platform="quark", name="账号", is_primary=True, status="active")
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.views = [view]
        adapter.dir_entries = {1: [DriveFile(file_id="tmp", name="mp_tr_busy", is_dir=True)]}
        removed = service._purge_temp_dirs(  # pylint: disable=protected-access
            adapter, NetdiskPlatform.QUARK, exclude=lambda: {"tmp"}
        )

    assert removed == 0
    assert adapter.deleted == []


def test_resolve_ensure_space_skip_paths(monkeypatch) -> None:
    """
    平台不返回配额、总容量未知或需求为 0 时跳过容量检查。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.drive_capabilities = DriveCapabilities()
        service._ensure_space(adapter, ShareFile(file_id="f", name="a", size=100), _record(1))  # pylint: disable=protected-access

        adapter.drive_capabilities = DriveCapabilities(quota=True)
        adapter.quota_value = DriveQuota(total=0)
        service._ensure_space(adapter, ShareFile(file_id="f", name="a", size=100), _record(1))  # pylint: disable=protected-access

        adapter.quota_value = DriveQuota(total=100, used=0)
        service._ensure_space(adapter, ShareFile(file_id="f", name="a", size=0), _record(3))  # pylint: disable=protected-access
        service._ensure_space(adapter, ShareFile(file_id="f", name="a", size=10), _record(4))  # pylint: disable=protected-access


def test_resolve_ensure_space_raises_when_full(monkeypatch) -> None:
    """
    剩余空间不足时冷却账号并抛出 SPACE_INSUFFICIENT。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.quota_value = DriveQuota(total=100, used=99)
        with pytest.raises(NetdiskError) as error:
            service._ensure_space(  # pylint: disable=protected-access
                adapter, ShareFile(file_id="f", name="a", size=10), _record(1)
            )

    assert error.value.code is NetdiskErrorCode.SPACE_INSUFFICIENT


def test_resolve_quota_failure_returns_zero(monkeypatch) -> None:
    """
    读取配额失败时按 0 处理，不阻断取链。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.quota_error = RuntimeError("quota failed")
        assert service._quota_for(adapter, _record(1)) == (0, 0)  # pylint: disable=protected-access


def test_resolve_accounts_with_explicit_id(monkeypatch) -> None:
    """
    指定账号时直接读取该账号记录。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        selected = service._resolve_accounts(adapter, 1, False)  # pylint: disable=protected-access

    assert [item.id for item in selected] == [1]


def test_resolve_select_primary_allows_guest(monkeypatch) -> None:
    """
    平台不要求登录且没有账号时返回游客。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        adapter.requires_account = False
        accounts.select_error = NetdiskError(
            NetdiskErrorCode.AUTH_EXPIRED, "没有可用账号", platform=NetdiskPlatform.QUARK
        )
        assert service._select_primary(adapter) is None  # pylint: disable=protected-access


def test_resolve_optional_account_by_id(monkeypatch) -> None:
    """
    只读操作指定账号时读取该账号。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        account = service._optional_account(adapter, 1)  # pylint: disable=protected-access

    assert account.id == 1


def test_resolve_refresh_failure_keeps_account(monkeypatch) -> None:
    """
    续期失败时保留原账号记录。
    """
    account = _record(1)
    with _resolve_env(monkeypatch) as (service, _adapter, accounts, _store):
        accounts.refresh_error = NetdiskError(NetdiskErrorCode.UNKNOWN, "boom")
        refreshed = service._refresh_account(account)  # pylint: disable=protected-access

    assert refreshed is account


def test_resolve_call_wraps_unexpected_error(monkeypatch) -> None:
    """
    协议层未分类异常收敛为 UNKNOWN。
    """
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        def _boom():
            """模拟协议层异常。"""
            raise RuntimeError("boom")

        with pytest.raises(NetdiskError) as error:
            service._call(adapter, _boom)  # pylint: disable=protected-access

    assert error.value.code is NetdiskErrorCode.UNKNOWN
    assert "RuntimeError" in error.value.message


def test_resolve_throttle_waits_between_calls(monkeypatch) -> None:
    """
    同一平台连续调用时补足最小间隔。
    """
    class _FakeTime:
        """可推进的单调时钟替身。"""

        def __init__(self) -> None:
            """从固定时间点开始。"""
            self.now = 100.0
            self.slept: list[float] = []

        def monotonic(self) -> float:
            """返回当前时间。"""
            return self.now

        def sleep(self, seconds: float) -> None:
            """记录并推进时间。"""
            self.slept.append(seconds)
            self.now += seconds

    fake = _FakeTime()
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        monkeypatch.setattr(resolve_module, "_PLATFORM_MIN_INTERVAL", 0.3)
        monkeypatch.setattr(resolve_module, "time", fake)
        service._last_call[NetdiskPlatform.QUARK] = 100.0  # pylint: disable=protected-access
        service._throttle(NetdiskPlatform.QUARK)  # pylint: disable=protected-access

    assert fake.slept == [pytest.approx(0.3)]


def test_resolve_cached_link_expired_is_dropped(monkeypatch) -> None:
    """
    剩余有效期低于缓存余量的直链不再复用。
    """
    key = (NetdiskPlatform.QUARK, "s1", "f1", 1)
    link = DirectLink(url="x", headers={}, expires_at=datetime.now() + timedelta(seconds=30))
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        service._link_cache[key] = link  # pylint: disable=protected-access
        assert service._cached_link(key) is None  # pylint: disable=protected-access
        assert key not in service._link_cache  # pylint: disable=protected-access


def test_resolve_remember_link_evicts_oldest_half(monkeypatch) -> None:
    """
    缓存达到上限时先淘汰最旧的一半。
    """
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        monkeypatch.setattr(resolve_module, "_LINK_CACHE_LIMIT", 2)
        for index in range(3):
            key = (NetdiskPlatform.QUARK, "s1", f"f{index}", 1)
            service._remember_link(key, DirectLink(url="x", headers={}))  # pylint: disable=protected-access

    assert len(service._link_cache) == 2  # pylint: disable=protected-access
    assert (NetdiskPlatform.QUARK, "s1", "f2", 1) in service._link_cache  # pylint: disable=protected-access


def test_resolve_tuning_falls_back_to_registry(monkeypatch) -> None:
    """
    未注入参数快照时读取注册端口。
    """
    with _resolve_env(monkeypatch) as (_service, _adapter, accounts, _store):
        service = NetdiskResolveService(accounts=accounts)
        assert service.tuning.auto_transfer is True


def test_resolve_reuses_cached_link(monkeypatch) -> None:
    """
    同一文件的第二次取链直接复用未过期缓存。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    ref = resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1")
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        first = service._resolve_one(adapter, ref, file, _record(1), force_refresh=False)  # pylint: disable=protected-access

        def _fail(*args, **kwargs):
            """缓存命中时不应再调用适配器。"""
            del args, kwargs
            raise AssertionError("不应重复取链")

        monkeypatch.setattr(adapter, "resolve", _fail)
        second = service._resolve_one(adapter, ref, file, _record(1), force_refresh=False)  # pylint: disable=protected-access

    assert second is first


def test_resolve_cached_link_returns_valid_entry(monkeypatch) -> None:
    """
    剩余有效期充足的缓存直链可直接复用。
    """
    key = (NetdiskPlatform.QUARK, "s1", "f1", 1)
    link = DirectLink(url="x", headers={}, expires_at=datetime.now() + timedelta(hours=1))
    with _resolve_env(monkeypatch) as (service, _adapter, _accounts, _store):
        service._link_cache[key] = link  # pylint: disable=protected-access
        assert service._cached_link(key) is link  # pylint: disable=protected-access


def test_resolve_transfer_first_sets_strategy(monkeypatch) -> None:
    """
    转存优先时取链结果标记 transfer 策略。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    ref = resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1")
    with _resolve_env(monkeypatch) as (_service, adapter, accounts, _store):
        accounts.select_result = _record(1)
        service = NetdiskResolveService(accounts=accounts, settings=_Settings(transfer_first=True))
        link = service._resolve_one(  # pylint: disable=protected-access
            adapter, ref, file, _record(1), force_refresh=False
        )

    assert link.strategy == "transfer"


def test_resolve_retries_after_auth_expired(monkeypatch) -> None:
    """
    首次取链登录态失效时续期账号并重试一次。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    ref = resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1")
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.resolve_errors = [
            NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "登录态失效", platform=NetdiskPlatform.QUARK)
        ]
        link = service._resolve_one(  # pylint: disable=protected-access
            adapter, ref, file, _record(1), force_refresh=False
        )

    assert link.url.endswith("/f1")


def test_resolve_reraises_other_classified_errors(monkeypatch) -> None:
    """
    限流等非可恢复错误直接向上抛出。
    """
    file = ShareFile(file_id="f1", name="a.mkv", size=10)
    ref = resolve_module.ShareRef(platform=NetdiskPlatform.QUARK, share_id="s1")
    with _resolve_env(monkeypatch) as (service, adapter, _accounts, _store):
        adapter.resolve_errors = [
            NetdiskError(NetdiskErrorCode.RATE_LIMITED, "太频繁", platform=NetdiskPlatform.QUARK)
        ]
        with pytest.raises(NetdiskError) as error:
            service._resolve_one(  # pylint: disable=protected-access
                adapter, ref, file, _record(1), force_refresh=False
            )

    assert error.value.code is NetdiskErrorCode.RATE_LIMITED


def test_resolve_transfer_required_purges_then_retries(monkeypatch) -> None:
    """
    必须转存且放不下时先清理临时目录再重试原账号。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        adapter.link_policy = LinkPolicy.TRANSFER_REQUIRED
        accounts.select_result = None
        calls: list[int] = []

        def _ensure(_adapter, _file, account, *, concurrency=1):
            """首次空间不足，清理后通过。"""
            del _adapter, _file, concurrency
            calls.append(account.id)
            if len(calls) == 1:
                raise NetdiskError(
                    NetdiskErrorCode.SPACE_INSUFFICIENT, "空间不足", platform=NetdiskPlatform.QUARK
                )

        monkeypatch.setattr(service, "_ensure_space", _ensure)
        adapter.dir_entries = {1: [DriveFile(file_id="tmp", name="mp_tr_old", is_dir=True)]}
        current, allow, strategy = service._select_transfer_target(  # pylint: disable=protected-access
            adapter, ShareFile(file_id="f", name="a.mkv", size=10), _record(1), 12, 1,
        )

    assert current.id == 1
    assert allow is True
    assert strategy == "transfer"
    assert adapter.deleted == [["tmp"]]


def test_resolve_select_fitting_returns_none_on_error(monkeypatch) -> None:
    """
    账号选择失败时返回 None，交由调用方决定回退。
    """
    with _resolve_env(monkeypatch) as (service, _adapter, accounts, _store):
        accounts.select_error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "没有可用账号")
        assert service._select_fitting(NetdiskPlatform.QUARK, 10, set()) is None  # pylint: disable=protected-access


def test_resolve_can_ever_fit_compares_totals(monkeypatch) -> None:
    """
    已知账号总容量时按最大容量判断需求是否可能满足。
    """
    view = NetdiskAccountView(
        id=1, platform="quark", name="账号", is_primary=True, status="active", quota_total=100
    )
    with _resolve_env(monkeypatch) as (service, _adapter, accounts, _store):
        accounts.views = [view]
        assert service._can_ever_fit(NetdiskPlatform.QUARK, 100) is True  # pylint: disable=protected-access
        assert service._can_ever_fit(NetdiskPlatform.QUARK, 200) is False  # pylint: disable=protected-access


def test_resolve_accounts_allows_guest_and_multi_source(monkeypatch) -> None:
    """
    只有「无可用账号」才回落游客；多源模式返回全部活跃账号。
    """
    view_primary = NetdiskAccountView(id=1, platform="quark", name="主账号", is_primary=True, status="active")
    view_second = NetdiskAccountView(id=2, platform="quark", name="备用", is_primary=False, status="active")
    view_expired = NetdiskAccountView(id=3, platform="quark", name="失效", is_primary=False, status="expired")
    with _resolve_env(monkeypatch) as (_service, adapter, accounts, _store):
        accounts.select_error = NetdiskError(
            NetdiskErrorCode.AUTH_EXPIRED, "没有可用账号", platform=NetdiskPlatform.QUARK
        )
        guests = _service._resolve_accounts(adapter, None, False, allow_guest=True)  # pylint: disable=protected-access

        accounts.select_error = None
        accounts.select_result = _record(1)
        accounts.views = [view_primary, view_second, view_expired]
        service = NetdiskResolveService(accounts=accounts, settings=_Settings(multi_source=True))
        selected = service._resolve_accounts(adapter, None, True)  # pylint: disable=protected-access

    assert guests == [None]
    assert [item.id for item in selected] == [1, 2]


def test_resolve_select_primary_returns_account(monkeypatch) -> None:
    """
    平台账号选择成功时返回主账号记录。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.select_result = _record(1)
        assert service._select_primary(adapter).id == 1  # pylint: disable=protected-access


def test_resolve_optional_account_defaults_to_primary(monkeypatch) -> None:
    """
    未指定账号时只读操作使用平台主账号。
    """
    with _resolve_env(monkeypatch) as (service, adapter, accounts, _store):
        accounts.select_result = _record(1)
        assert service._optional_account(adapter, None).id == 1  # pylint: disable=protected-access


class _FilesService:
    """
    个人盘文件接口用例替身。
    """

    def __init__(self) -> None:
        """
        创建可编排的替身。
        """
        self.error: Exception | None = None
        self.calls: list[tuple] = []

    def _check(self) -> None:
        """按需抛出预设异常。"""
        if self.error is not None:
            raise self.error

    def capabilities(self, account_id):
        """返回全能力声明。"""
        self._check()
        self.calls.append(("capabilities", account_id))
        return DriveCapabilities(quota=True, browse=True, download=True, share=True)

    def quota(self, account_id):
        """返回固定配额。"""
        self._check()
        return DriveQuota(used=1, total=2)

    def list_dir(self, account_id, parent_id):
        """返回固定目录条目。"""
        self._check()
        self.calls.append(("list_dir", account_id, parent_id))
        return [
            DriveFile(
                file_id="f1",
                name="a.mkv",
                size=3,
                updated_at=datetime(2026, 1, 2, 3, 4, 5),
                parent_id="",
            )
        ]

    def share(self, account_id, files, *, password="", expires_in=0):
        """返回固定分享结果。"""
        self._check()
        self.calls.append(("share", account_id, [item.file_id for item in files], password, expires_in))
        return DriveShare(
            url="https://pan.quark.cn/s/new",
            password=password,
            expires_at=datetime(2026, 2, 3, 4, 5, 6),
        )

    def move(self, account_id, files, target_id):
        """记录移动调用。"""
        self._check()
        self.calls.append(("move", account_id, [item.file_id for item in files], target_id))

    def rename(self, account_id, file, name):
        """记录重命名调用。"""
        self._check()
        self.calls.append(("rename", account_id, file.file_id, name))

    def delete(self, account_id, files):
        """记录删除调用。"""
        self._check()
        self.calls.append(("delete", account_id, [item.file_id for item in files]))

    def download(self, account_id, file):
        """返回固定直链。"""
        self._check()
        return DirectLink(url="https://cdn.example.com/stream", headers={"Cookie": "a=1"}, size=file.size)


class _FakeHttpResponse:
    """
    下载代理响应替身。
    """

    status_code = 206
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": "3",
        "Content-Range": "bytes 0-2/3",
    }

    def __init__(self) -> None:
        """
        记录是否已释放连接。
        """
        self.closed = False

    def iter_content(self, chunk_size: int):
        """
        返回两段响应体，其中一段为空。
        """
        del chunk_size
        yield b"abc"
        yield b""

    def close(self) -> None:
        """
        标记连接已释放。
        """
        self.closed = True


class _FakeRequestUtils:
    """
    下载请求工具替身。
    """

    response: object = None
    last: dict | None = None

    def __init__(self, headers=None, timeout=0) -> None:
        """
        保存请求头与超时。
        """
        self.headers = headers
        self.timeout = timeout

    def get_res(self, url, **kwargs):
        """
        记录调用并返回预设响应。
        """
        _FakeRequestUtils.last = {"url": url, "kwargs": kwargs, "headers": self.headers}
        return _FakeRequestUtils.response


@contextmanager
def _files_client(monkeypatch, service):
    """
    装配个人盘文件路由与依赖覆盖。
    """
    monkeypatch.setattr(files_api, "_service", lambda: service)
    app = FastAPI()
    app.include_router(files_api.router, prefix="/api/v1/netdisk/files")
    app.dependency_overrides[files_api.get_current_active_superuser_async] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_files_api_capabilities_and_quota(monkeypatch) -> None:
    """
    能力与空间接口返回统一成功响应。
    """
    service = _FilesService()
    with _files_client(monkeypatch, service) as client:
        caps = client.get("/api/v1/netdisk/files/1/capabilities").json()
        quota = client.get("/api/v1/netdisk/files/1/quota").json()

    assert caps["success"] is True
    assert caps["data"]["capabilities"]["download"] is True
    assert quota["data"]["quota"] == {"used": 1, "total": 2}


def test_files_api_list_and_error(monkeypatch) -> None:
    """
    目录接口返回条目与修改时间；分类异常转为失败响应。
    """
    service = _FilesService()
    with _files_client(monkeypatch, service) as client:
        body = client.get("/api/v1/netdisk/files/1/list", params={"parent_id": "d1"}).json()
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持目录浏览")
        failed = client.get("/api/v1/netdisk/files/1/list").json()

    assert body["success"] is True
    assert body["data"]["files"][0]["updated_at"] == "2026-01-02T03:04:05"
    assert failed["success"] is False
    assert failed["data"]["code"] == "unsupported"


def test_files_api_service_factory() -> None:
    """
    默认服务工厂构造个人盘用例实例。
    """
    assert isinstance(files_api._service(), NetdiskFilesService)  # pylint: disable=protected-access


def test_files_api_unclassified_error_is_wrapped(monkeypatch) -> None:
    """
    未分类异常收敛为统一失败响应，避免暴露 500 插画。
    """
    service = _FilesService()
    service.error = RuntimeError("boom")
    with _files_client(monkeypatch, service) as client:
        body = client.get("/api/v1/netdisk/files/1/capabilities").json()

    assert body["success"] is False
    assert body["data"]["code"] == "unknown"
    assert body["message"] == "网盘接口异常，请稍后重试"


def test_files_api_each_endpoint_reports_failure(monkeypatch) -> None:
    """
    各文件操作接口把分类异常转为失败响应。
    """
    service = _FilesService()
    payload = {"file_id": "f1", "name": "a.mkv", "size": 3}
    with _files_client(monkeypatch, service) as client:
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持空间查询")
        quota = client.get("/api/v1/netdisk/files/1/quota").json()
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持创建分享")
        share = client.post("/api/v1/netdisk/files/1/share", json={"files": [payload]}).json()
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持移动文件")
        move = client.post(
            "/api/v1/netdisk/files/1/move", json={"files": [payload], "target_id": "t"}
        ).json()
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持重命名")
        rename = client.post(
            "/api/v1/netdisk/files/1/rename", json={"file": payload, "name": "b.mkv"}
        ).json()
        service.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持删除")
        delete = client.post("/api/v1/netdisk/files/1/delete", json={"files": [payload]}).json()

    for body in (quota, share, move, rename, delete):
        assert body["success"] is False
        assert body["data"]["code"] == "unsupported"


def test_files_api_share_move_rename_delete(monkeypatch) -> None:
    """
    分享、移动、重命名与删除接口按请求体转发用例。
    """
    service = _FilesService()
    payload = {"file_id": "f1", "name": "a.mkv", "size": 3}
    with _files_client(monkeypatch, service) as client:
        share = client.post(
            "/api/v1/netdisk/files/1/share",
            json={"files": [payload], "password": "abcd", "expires_in": 60},
        ).json()
        moved = client.post(
            "/api/v1/netdisk/files/1/move", json={"files": [payload], "target_id": "t1"}
        ).json()
        renamed = client.post(
            "/api/v1/netdisk/files/1/rename", json={"file": payload, "name": "b.mkv"}
        ).json()
        deleted = client.post(
            "/api/v1/netdisk/files/1/delete", json={"files": [payload]}
        ).json()

    assert share["data"]["share"]["password"] == "abcd"
    assert share["data"]["share"]["expires_at"] == "2026-02-03T04:05:06"
    assert moved["success"] is True
    assert renamed["success"] is True
    assert deleted["success"] is True
    assert service.calls[-1] == ("delete", 1, ["f1"])


def test_files_api_stream_forwards_range(monkeypatch) -> None:
    """
    代理下载透传 Range 请求头与上游响应头。
    """
    response = _FakeHttpResponse()
    _FakeRequestUtils.response = response
    monkeypatch.setattr(files_api, "RequestUtils", _FakeRequestUtils)
    service = _FilesService()
    with _files_client(monkeypatch, service) as client:
        result = client.get(
            "/api/v1/netdisk/files/1/stream/f1",
            params={"name": "a.mkv", "size": 3},
            headers={"Range": "bytes=0-2"},
        )

    assert result.status_code == 206
    assert result.content == b"abc"
    assert result.headers["accept-ranges"] == "bytes"
    assert result.headers["content-range"] == "bytes 0-2/3"
    assert "filename*=UTF-8''a.mkv" in result.headers["content-disposition"]
    assert _FakeRequestUtils.last["kwargs"]["headers"] == {"Range": "bytes=0-2"}
    assert response.closed is True


def test_files_api_stream_failure_returns_500(monkeypatch) -> None:
    """
    上游取流失败时抛出分类异常。
    """
    _FakeRequestUtils.response = None
    monkeypatch.setattr(files_api, "RequestUtils", _FakeRequestUtils)
    with _files_client(monkeypatch, _FilesService()) as client:
        result = client.get("/api/v1/netdisk/files/1/stream/f1")

    assert result.status_code == 500


class _AccountsService:
    """
    账号接口用例替身。
    """

    def __init__(self) -> None:
        """
        创建可编排的替身。
        """
        self.error: Exception | None = None
        self.view = NetdiskAccountView(
            id=1,
            platform="quark",
            name="主账号",
            is_primary=True,
            status="active",
            expires_at=datetime(2026, 1, 1),
            last_checked_at=datetime(2026, 1, 2),
            masked={"cookie": "***"},
            nickname="昵称",
            quota_used=1,
            quota_total=2,
        )
        self.store = SimpleNamespace(get_record=lambda account_id: None)

    def _check(self) -> None:
        """按需抛出预设异常。"""
        if self.error is not None:
            raise self.error

    def list(self, platform=None):
        """返回预设账号列表。"""
        del platform
        self._check()
        return [self.view]

    def check_all(self):
        """返回固定检测统计。"""
        self._check()
        return {"checked": 1, "active": 1, "expired": 0, "failed": 0}

    def clear_recycle_bin(self, account_id):
        """返回固定清理结果。"""
        del account_id
        self._check()
        return {"cleared": True, "empty": False, "message": "回收站已清空", "cleared_at": "2026-01-01"}


class _LoginService:
    """
    登录接口用例替身。
    """

    def __init__(self) -> None:
        """
        创建可编排的替身。
        """
        self.error: Exception | None = None
        self.view = NetdiskAccountView(
            id=1, platform="quark", name="主账号", is_primary=True, status="active"
        )
        self.statuses: list[str] = []
        self.events: list[tuple] = []
        self.streams: list[tuple] = []
        self.stopped: list[str] = []
        self.stream_frames: list[tuple[bytes, dict]] = []

    def _check(self) -> None:
        """按需抛出预设异常。"""
        if self.error is not None:
            raise self.error

    def paste_cookie(self, platform, cookie, *, name=None, is_primary=False, account_id=None):
        """返回固定账号视图。"""
        del platform, cookie, name, is_primary, account_id
        self._check()
        return self.view

    def import_cookiecloud(self, platform, *, name=None, is_primary=False, account_id=None):
        """返回固定账号视图。"""
        del platform, name, is_primary, account_id
        self._check()
        return self.view

    def password_login(self, platform, username, password, *, name=None, is_primary=False, account_id=None):
        """返回固定账号视图。"""
        del platform, username, password, name, is_primary, account_id
        self._check()
        return self.view

    def send_sms_code(self, platform, phone, *, account_id=None):
        """返回固定发码结果。"""
        del platform, phone, account_id
        self._check()
        return {"verification_id": "v-1", "is_user": True, "expires_in": 300}

    def sms_login(self, platform, phone, code, verification_id, *, name=None, is_primary=False, account_id=None):
        """返回固定账号视图。"""
        del platform, phone, code, verification_id, name, is_primary, account_id
        self._check()
        return self.view

    def start_qr(self, platform, *, name=None, is_primary=False, account_id=None):
        """返回固定登录会话。"""
        del name, is_primary, account_id
        self._check()
        return LoginSession(
            session_id="s-1",
            platform=NetdiskPlatform(platform),
            image=b"qr-png",
            page_image=b"page-png",
            status="pending",
            expires_at=datetime(2026, 1, 1),
        )

    def poll_qr(self, session_id):
        """按队列返回轮询结果。"""
        del session_id
        self._check()
        status = self.statuses.pop(0) if self.statuses else "pending"
        return LoginResult(status=status, message=status, account_name="昵称")

    def confirm_qr(self, session_id):
        """返回固定账号视图。"""
        del session_id
        self._check()
        return self.view

    def cancel_qr(self, session_id):
        """记录取消调用。"""
        del session_id
        self._check()

    def qr_frame(self, session_id):
        """返回固定画面。"""
        del session_id
        self._check()
        return {"image": b"img", "width": 10, "height": 20}

    def qr_input(self, session_id, **kwargs):
        """返回固定画面。"""
        del session_id, kwargs
        self._check()
        return {"image": b"img", "width": 10, "height": 20}

    def credential_from_text(self, platform, text):
        """返回固定凭据。"""
        del platform, text
        self._check()
        return {"token": "jwt"}

    def qr_stream_start(self, session_id, on_frame):
        """记录实时画面启动并推入预设帧。"""
        self.streams.append((session_id, on_frame))
        for frame in self.stream_frames:
            on_frame(*frame)

    def qr_stream_stop(self, session_id):
        """记录实时画面停止。"""
        self.stopped.append(session_id)

    def qr_send_event(self, session_id, event):
        """记录页面事件。"""
        self.events.append((session_id, event))


@contextmanager
def _netdisk_client(monkeypatch, accounts=None, login=None):
    """
    装配账号路由、依赖覆盖与模块替身。
    """
    accounts = accounts or _AccountsService()
    login = login or _LoginService()
    monkeypatch.setattr(netdisk_api, "_accounts", lambda: accounts)
    monkeypatch.setattr(netdisk_api, "_login", lambda: login)
    app = FastAPI()
    app.include_router(netdisk_api.router, prefix="/api/v1/netdisk")
    app.dependency_overrides[netdisk_api.get_current_active_superuser_async] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, accounts, login


@pytest.fixture(autouse=True)
def _reset_ticket_tables():
    """
    每个用例前后清空票据表，避免跨用例污染。
    """
    netdisk_api._QR_TICKETS.clear()
    netdisk_api._VNC_TICKETS.clear()
    yield
    netdisk_api._QR_TICKETS.clear()
    netdisk_api._VNC_TICKETS.clear()


def test_netdisk_api_list_accounts_and_error(monkeypatch) -> None:
    """
    账号列表返回脱敏信息；分类异常转为失败响应。
    """
    with _netdisk_client(monkeypatch) as (client, accounts, _login):
        body = client.get("/api/v1/netdisk/accounts").json()
        accounts.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "未知的网盘平台")
        failed = client.get("/api/v1/netdisk/accounts").json()

    assert body["success"] is True
    assert body["data"]["accounts"][0]["expires_at"] == "2026-01-01T00:00:00"
    assert body["data"]["accounts"][0]["speed"] == 0.0
    assert failed["success"] is False
    assert failed["data"]["code"] == "unsupported"


def test_netdisk_api_cookie_endpoints(monkeypatch) -> None:
    """
    粘贴 Cookie 与 CookieCloud 导入接口返回账号；异常转为失败响应。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, login):
        cookie = client.post(
            "/api/v1/netdisk/accounts/cookie", json={"platform": "quark", "cookie": "a=1"}
        ).json()
        login.error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "Cookie 无效")
        failed = client.post(
            "/api/v1/netdisk/accounts/cookie", json={"platform": "quark", "cookie": "a=1"}
        ).json()
        login.error = None
        cloud = client.post(
            "/api/v1/netdisk/accounts/cookiecloud", json={"platform": "quark"}
        ).json()
        login.error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "CookieCloud 未配置")
        cloud_failed = client.post(
            "/api/v1/netdisk/accounts/cookiecloud", json={"platform": "quark"}
        ).json()

    assert cookie["data"]["account"]["id"] == 1
    assert failed["data"]["code"] == "auth_expired"
    assert cloud["success"] is True
    assert cloud_failed["success"] is False


def test_netdisk_api_sms_endpoints(monkeypatch) -> None:
    """
    短信发码与登录接口返回验证会话与账号。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, login):
        sent = client.post(
            "/api/v1/netdisk/accounts/sms/send", json={"platform": "guangya", "phone": "138"}
        ).json()
        login.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持短信验证码登录")
        send_failed = client.post(
            "/api/v1/netdisk/accounts/sms/send", json={"platform": "quark", "phone": "138"}
        ).json()
        login.error = None
        logged = client.post(
            "/api/v1/netdisk/accounts/sms/login",
            json={"platform": "guangya", "phone": "138", "code": "1234", "verification_id": "v-1"},
        ).json()
        login.error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "短信验证码登录失败")
        login_failed = client.post(
            "/api/v1/netdisk/accounts/sms/login",
            json={"platform": "guangya", "phone": "138", "code": "1234", "verification_id": "v-1"},
        ).json()

    assert sent["data"]["sms_verification_id"] == "v-1"
    assert sent["data"]["sms_is_user"] is True
    assert sent["data"]["sms_expires_in"] == 300
    assert send_failed["data"]["code"] == "unsupported"
    assert logged["data"]["account"]["id"] == 1
    assert login_failed["data"]["code"] == "auth_expired"


def test_netdisk_api_qr_start_poll_confirm(monkeypatch) -> None:
    """
    扫码启动、轮询与确认接口返回会话、状态与账号。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, login):
        netdisk_api._VNC_TICKETS["stale"] = ("s-1", time.monotonic() - 1)
        started = client.post(
            "/api/v1/netdisk/accounts/qr", json={"platform": "quark"}
        ).json()
        stale_removed = "stale" not in netdisk_api._VNC_TICKETS
        login.statuses = ["pending", "success"]
        pending = client.get("/api/v1/netdisk/accounts/qr/s-1").json()
        success = client.get("/api/v1/netdisk/accounts/qr/s-1").json()
        login.error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "登录会话不存在或已过期")
        poll_failed = client.get("/api/v1/netdisk/accounts/qr/s-1").json()
        login.error = None
        confirmed = client.post("/api/v1/netdisk/accounts/qr/s-1/confirm").json()
        login.error = NetdiskError(NetdiskErrorCode.AUTH_EXPIRED, "没有待确认的登录态")
        confirm_failed = client.post("/api/v1/netdisk/accounts/qr/s-1/confirm").json()

    assert started["data"]["session_id"] == "s-1"
    assert stale_removed is True
    assert started["data"]["image"] == "qr-png"
    assert started["data"]["page_image"] == "page-png"
    assert started["data"]["vnc_path"].startswith("/api/v1/netdisk/accounts/qr/s-1/vnc/")
    assert pending["success"] is False
    assert pending["data"]["status"] == "pending"
    assert success["success"] is True
    assert poll_failed["success"] is False
    assert confirmed["data"]["account"]["id"] == 1
    assert confirm_failed["success"] is False


def test_netdisk_api_qr_start_error(monkeypatch) -> None:
    """
    启动扫码失败时返回失败响应。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, login):
        login.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "该平台不支持扫码登录")
        failed = client.post("/api/v1/netdisk/accounts/qr", json={"platform": "quark"}).json()

    assert failed["success"] is False
    assert failed["data"]["code"] == "unsupported"


def test_netdisk_api_qr_ticket_and_frame(monkeypatch) -> None:
    """
    实时画面票据签发与当前画面读取。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, login):
        ticket = client.post("/api/v1/netdisk/accounts/qr/s-1/ticket").json()
        frame = client.get("/api/v1/netdisk/accounts/qr/s-1/frame").json()
        login.error = NetdiskError(NetdiskErrorCode.UNSUPPORTED, "当前登录方式不支持页面交互")
        failed = client.get("/api/v1/netdisk/accounts/qr/s-1/frame").json()

    assert ticket["data"]["session_id"] == "s-1"
    assert ticket["data"]["ticket"] in netdisk_api._QR_TICKETS
    assert frame["data"]["image"] == "aW1n"
    assert frame["data"]["page_width"] == 10
    assert failed["success"] is False


def test_netdisk_api_qr_input_forwards_parameters(monkeypatch) -> None:
    """
    页面操作接口透传点击参数并返回最新画面。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, _login):
        body = client.post(
            "/api/v1/netdisk/accounts/qr/s-1/input",
            json={"kind": "click", "x": 1, "y": 2},
        ).json()

    assert body["success"] is True
    assert body["data"]["page_height"] == 20


def test_vnc_websocket_rejects_invalid_ticket(monkeypatch) -> None:
    """
    无效票据直接关闭连接。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, _login):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/api/v1/netdisk/accounts/qr/s-1/vnc/bad") as websocket:
                websocket.receive_text()

    assert error.value.code == 4401


def test_vnc_websocket_proxies_text_frames(monkeypatch) -> None:
    """
    有效票据时代理上游 VNC 文本帧，并在结束后关闭上游。
    """
    class _Upstream:
        """VNC 上游连接替身。"""

        def __init__(self) -> None:
            """初始化待发消息。"""
            self.messages: list[object] = ["hello"]
            self.sent: list[bytes] = []
            self.closed = False

        async def send(self, data):
            """记录浏览器发往上游的帧。"""
            self.sent.append(data)

        async def close(self):
            """标记上游已关闭。"""
            self.closed = True

        def __aiter__(self):
            """返回自身作为异步迭代器。"""
            return self

        async def __anext__(self):
            """依次返回待发消息，耗尽后模拟上游中断。"""
            if not self.messages:
                raise RuntimeError("upstream closed")
            return self.messages.pop(0)

    upstream = _Upstream()

    async def _connect(url, **kwargs):
        """返回预设上游连接。"""
        del url, kwargs
        return upstream

    monkeypatch.setattr(netdisk_api.websockets, "connect", _connect)
    ticket = netdisk_api._issue_vnc_ticket("s-1")
    with _netdisk_client(monkeypatch) as (client, _accounts, _login):
        with client.websocket_connect(
            f"/api/v1/netdisk/accounts/qr/s-1/vnc/{ticket}"
        ) as websocket:
            assert websocket.receive_text() == "hello"

    assert upstream.closed is True


def test_vnc_websocket_closes_when_upstream_unavailable(monkeypatch) -> None:
    """
    上游 VNC 服务不可用时以 1011 关闭连接。
    """

    async def _connect(url, **kwargs):
        """模拟连接失败。"""
        del url, kwargs
        raise OSError("connection refused")

    monkeypatch.setattr(netdisk_api.websockets, "connect", _connect)
    ticket = netdisk_api._issue_vnc_ticket("s-1")
    with _netdisk_client(monkeypatch) as (client, _accounts, _login):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                f"/api/v1/netdisk/accounts/qr/s-1/vnc/{ticket}"
            ) as websocket:
                websocket.receive_text()

    assert error.value.code == 1011


def test_qr_stream_websocket_rejects_invalid_ticket(monkeypatch) -> None:
    """
    实时画面票据无效时关闭连接。
    """
    with _netdisk_client(monkeypatch) as (client, _accounts, _login):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/api/v1/netdisk/accounts/qr/s-1/ws?ticket=bad") as websocket:
                websocket.receive_json()

    assert error.value.code == 4401


def test_qr_stream_websocket_pushes_frames_and_events(monkeypatch) -> None:
    """
    实时画面先推送帧，再同步状态；前端事件转发到登录端口。
    """
    login = _LoginService()
    login.statuses = ["pending", "failed"]
    login.stream_frames = [
        (b"frame-1", {"deviceWidth": 100, "deviceHeight": 50}),
        (b"frame-2", {"deviceWidth": 100, "deviceHeight": 50}),
        (b"frame-3", {"deviceWidth": 100, "deviceHeight": 50}),
        (b"frame-4", {"deviceWidth": 100, "deviceHeight": 50}),
    ]
    netdisk_api._QR_TICKETS["ticket-1"] = ("s-1", time.monotonic() + 60)
    with _netdisk_client(monkeypatch, login=login) as (client, _accounts, _service):
        with client.websocket_connect(
            "/api/v1/netdisk/accounts/qr/s-1/ws?ticket=ticket-1"
        ) as websocket:
            frames = []
            pending = None
            while True:
                message = websocket.receive_json()
                if message["type"] != "status":
                    frames.append(message)
                    continue
                if pending is None:
                    pending = message
                    websocket.send_json({"kind": "click", "x": 1, "y": 2})
                    continue
                finished = message
                break

    assert len(frames) == 3
    assert frames[0]["data"] == base64.b64encode(b"frame-2").decode("ascii")
    assert frames[0]["width"] == 100
    assert frames[0]["height"] == 50
    assert pending["status"] == "pending"
    assert finished["status"] == "failed"
    assert login.events == [("s-1", {"kind": "click", "x": 1, "y": 2})]
    assert login.stopped == ["s-1"]
