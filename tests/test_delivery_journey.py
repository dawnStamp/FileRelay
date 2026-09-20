"""寄件授权回归：真实路由、内存数据库和隔离文件，覆盖越权与额度生命周期。"""

import asyncio
from datetime import timedelta

import pytest

from apps.admin.dependencies import create_token
from apps.base.config import refresh_settings
from apps.base.models import DeliveryCode, FileCodes, KeyValue, StorageReservation, UploadChunk
from apps.base.quota import get_storage_usage
from apps.base.upload_sessions import cleanup_once
from core.settings import settings
from core.storage import SystemFileStorage
from core.utils import get_now


@pytest.fixture
async def delivery_env(client, tmp_path, monkeypatch):
    """不用会删除 data/share 的旧初始化 fixture，文件仅进入当前用例临时目录。"""
    original = dict(settings.user_config)
    monkeypatch.setattr("core.storage.data_root", tmp_path)
    try:
        response = await client.post("/setup", json={
            "admin_password": "Delivery-Test-12345", "confirm_password": "Delivery-Test-12345",
            "site_name": "寄件回归", "expire_style": ["day", "forever", "count"],
        }, headers={"Accept": "application/json"})
        assert response.status_code == 200, response.text
        login = await client.post("/admin/login", json={"password": "Delivery-Test-12345"})
        assert login.status_code == 200, login.text
        admin = {"Authorization": f"Bearer {login.json()['detail']['token']}"}
        await set_config(open_upload=0, upload_count=1000, error_count=1000, storage_limit=0)
        yield client, admin
    finally:
        settings.user_config = original


async def set_config(**changes):
    """同步数据库和配置缓存，确保真实请求使用该用例的站点限制。"""
    row = await KeyValue.get(key="settings")
    row.value = {**row.value, **changes}
    await row.save()
    await refresh_settings(force=True)


async def create_delivery(env, **changes):
    """经管理员接口创建并经公开接口验证，避免绕过实际鉴权链。"""
    client, admin = env
    response = await client.post("/admin/delivery/codes", headers=admin, json={
        "name": "回归收件", "expires_at": (await get_now() + timedelta(days=1)).isoformat(),
        "max_uploads": 3, **changes,
    })
    assert response.status_code == 201, response.text
    created = response.json()["detail"]
    verified = await client.post("/api/delivery/verify", json={"code": created["code"]})
    assert verified.status_code == 200, verified.text
    assert verified.headers["cache-control"] == "no-store"
    return created, {"Authorization": f"Bearer {verified.json()['detail']['token']}"}


async def send_text(client, headers, text="寄件回归"):
    return await client.post("/share/text/", headers=headers,
                             data={"text": text, "expire_value": 1, "expire_style": "day"})


async def init_upload(client, headers, kind="chunk", size=8, name="parcel.bin"):
    """两种上传会话都必须经过统一的寄件鉴权及次数/容量预占。"""
    if kind == "chunk":
        return await client.post("/chunk/upload/init/", headers=headers, json={
            "file_name": name, "file_size": size, "chunk_size": 4, "file_hash": "0" * 64,
        })
    return await client.post("/presign/upload/init", headers=headers, json={
        "file_name": name, "file_size": size, "expire_value": 1, "expire_style": "day",
    })


@pytest.mark.parametrize("kind", ["text", "file", "chunk", "presign"])
async def test_closed_guest_upload_accepts_delivery_but_rejects_anonymous(delivery_env, kind):
    """关闭游客上传不能封死合法寄件，也不能误放行无凭证请求。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env)

    async def request(auth):
        if kind == "text":
            return await send_text(client, auth)
        if kind == "file":
            return await client.post("/share/file/", headers=auth,
                                     files={"file": ("parcel.txt", b"delivery", "text/plain")})
        return await init_upload(client, auth, kind)

    assert (await request({})).status_code == 403
    response = await request(headers)
    assert response.status_code == 200, response.text
    record = await DeliveryCode.get(id=created["item"]["id"])
    assert (record.used_count, record.reserved_count) == ((1, 0) if kind in {"text", "file"} else (0, 1))


async def test_delivery_token_cannot_access_admin_and_admin_upload_stays_normal(delivery_env):
    """用途隔离必须阻止寄件者访问后台，管理员普通上传不能误关联寄件码。"""
    client, admin = delivery_env
    _, delivery = await create_delivery(delivery_env)
    for path in ("/admin/delivery/codes", "/admin/file/list/"):
        response = await client.get(path, headers=delivery)
        assert response.status_code in (401, 403), response.text
    response = await client.post("/api/delivery/refresh", headers=admin)
    assert response.status_code == 401
    response = await send_text(client, admin)
    assert response.status_code == 200, response.text
    share = await FileCodes.get(code=response.json()["detail"]["code"])
    assert share.delivery_id is None


@pytest.mark.parametrize("kind", ["chunk", "presign"])
@pytest.mark.parametrize("intruder", ["other_code", "admin", "guest"])
async def test_upload_sessions_reject_other_identities(delivery_env, kind, intruder):
    """即使游客上传开放，其他寄件码、管理员和游客也不能读写或取消已有会话。"""
    client, admin = delivery_env
    _, owner = await create_delivery(delivery_env)
    _, other = await create_delivery(delivery_env)
    await set_config(open_upload=1)
    response = await init_upload(client, owner, kind)
    assert response.status_code == 200, response.text
    upload_id = response.json()["detail"]["upload_id"]
    headers = {"other_code": other, "admin": admin, "guest": {}}[intruder]
    prefix = f"/{kind}/upload"
    # HTTP 404 不能被 SPA 回退变成 HTML 200，否则客户端会把越权当成功。
    status = await client.get(f"{prefix}/status/{upload_id}", headers=headers)
    assert status.status_code == 404, status.text
    if kind == "chunk":
        write = await client.post(f"{prefix}/chunk/{upload_id}/0", headers=headers,
                                  files={"chunk": ("part", b"abcd")})
    else:
        write = await client.put(f"{prefix}/proxy/{upload_id}", headers=headers,
                                 files={"file": ("parcel.bin", b"abcdefgh")})
    assert write.status_code == 404, write.text
    cancel = await client.delete(f"{prefix}/{upload_id}", headers=headers)
    assert cancel.status_code == 404, cancel.text
    assert await StorageReservation.filter(token=upload_id).exists()
    assert (await client.get(f"{prefix}/status/{upload_id}", headers=owner)).status_code == 200


@pytest.mark.parametrize("action", ["recode", "disable", "delete", "expire"])
async def test_revoked_authorization_cannot_refresh_or_finish_upload(delivery_env, action):
    """撤销后立即拒绝完成，未完成会话的预留在到期清理后归还。"""
    client, admin = delivery_env
    created, headers = await create_delivery(delivery_env)
    code_id = created["item"]["id"]
    response = await init_upload(client, headers, "presign")
    assert response.status_code == 200, response.text
    upload_url = response.json()["detail"]["upload_url"]
    if action == "recode":
        response = await client.put(f"/admin/delivery/codes/{code_id}", headers=admin,
                                    json={"code": "Replacement_123"})
        assert response.status_code == 200, response.text
    elif action == "disable":
        response = await client.patch(f"/admin/delivery/codes/{code_id}", headers=admin, json={"enabled": False})
        assert response.status_code == 200, response.text
    elif action == "delete":
        assert (await client.delete(f"/admin/delivery/codes/{code_id}", headers=admin)).status_code == 200
    else:
        await DeliveryCode.filter(id=code_id).update(expires_at=await get_now() - timedelta(seconds=1))
    assert (await client.post("/api/delivery/refresh", headers=headers)).status_code in (401, 403)
    response = await client.put(upload_url, headers=headers, files={"file": ("parcel.bin", b"abcdefgh")})
    assert response.status_code in (401, 403), response.text
    assert (await DeliveryCode.get(id=code_id)).used_count == 0
    assert await FileCodes.all().count() == 0
    # 撤销凭证不能提前释放仍可能写入的物理文件容量；推进租约后验证最终回收。
    assert (await DeliveryCode.get(id=code_id)).reserved_count == 1
    assert (await get_storage_usage())["reserved"] == 8
    await StorageReservation.filter(delivery_id=code_id).update(expires_at=await get_now() - timedelta(seconds=1))
    await cleanup_once()
    assert (await DeliveryCode.get(id=code_id)).reserved_count == 0
    assert (await get_storage_usage())["reserved"] == 0
    assert not await StorageReservation.filter(delivery_id=code_id).exists()


async def test_refresh_allows_continuation_and_expired_token_is_rejected(delivery_env):
    """续期应允许继续原会话；已过期 JWT 必须重新验证，不能直接续期。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env)
    started = await init_upload(client, headers, "presign")
    refreshed = await client.post("/api/delivery/refresh", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.headers["cache-control"] == "no-store"
    current = {"Authorization": f"Bearer {refreshed.json()['detail']['token']}"}
    uploaded = await client.put(started.json()["detail"]["upload_url"], headers=current,
                                files={"file": ("parcel.bin", b"abcdefgh")})
    assert uploaded.status_code == 200, uploaded.text
    expired = create_token({"purpose": "delivery", "delivery_id": created["item"]["id"],
                            "delivery_version": 1}, expires_in=-1)
    expired_headers = {"Authorization": f"Bearer {expired}"}
    assert (await client.post("/api/delivery/refresh", headers=expired_headers)).status_code == 401
    assert (await send_text(client, expired_headers)).status_code == 401


async def test_concurrent_uploads_cannot_reserve_more_than_last_slot(delivery_env):
    """真实并发请求抢最后一次额度，仅一个成功，失败请求不留额外预占。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env, max_uploads=1)
    results = await asyncio.gather(*(init_upload(client, headers, "presign", name=f"p{i}.bin") for i in range(4)))
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]
    record = await DeliveryCode.get(id=created["item"]["id"])
    assert (record.used_count, record.reserved_count) == (0, 1)
    assert await StorageReservation.all().count() == 1


@pytest.mark.parametrize("kind", ["chunk", "presign"])
async def test_capacity_failure_releases_slot_and_cancel_allows_retry(delivery_env, kind):
    """容量不足的初始化不能占住次数；取消有效会话后，容量和次数均可再次使用。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env, max_uploads=1)
    await set_config(storage_limit=7)
    response = await init_upload(client, headers, kind)
    assert response.status_code == 507, response.text
    record = await DeliveryCode.get(id=created["item"]["id"])
    assert (record.used_count, record.reserved_count) == (0, 0)
    assert await StorageReservation.all().count() == 0
    await set_config(storage_limit=8)
    response = await init_upload(client, headers, kind)
    assert response.status_code == 200, response.text
    assert (await get_storage_usage())["reserved"] == 8
    upload_id = response.json()["detail"]["upload_id"]
    cancelled = await client.delete(f"/{kind}/upload/{upload_id}", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert (await get_storage_usage())["reserved"] == 0
    assert (await init_upload(client, headers, kind)).status_code == 200


@pytest.mark.parametrize("kind", ["chunk", "presign"])
async def test_success_retry_counts_once_and_download_survives_code_deletion(delivery_env, kind):
    """完成响应丢失后重试不能重复扣次；耗尽与软删不影响已生成分享。"""
    client, admin = delivery_env
    created, headers = await create_delivery(delivery_env, max_uploads=1)
    code_id = created["item"]["id"]
    response = await init_upload(client, headers, kind)
    assert response.status_code == 200, response.text
    upload_id = response.json()["detail"]["upload_id"]
    if kind == "chunk":
        for index, payload in enumerate([b"abcd", b"efgh"]):
            part = await client.post(f"/chunk/upload/chunk/{upload_id}/{index}", headers=headers,
                                     files={"chunk": ("part", payload)})
            assert part.status_code == 200, part.text

    async def complete():
        if kind == "chunk":
            return await client.post(f"/chunk/upload/complete/{upload_id}", headers=headers,
                                      json={"expire_value": 1, "expire_style": "day"})
        return await client.put(f"/presign/upload/proxy/{upload_id}", headers=headers,
                                 files={"file": ("parcel.bin", b"abcdefgh")})

    first = await complete()
    assert first.status_code == 200, first.text
    retry = await complete()
    assert retry.status_code == 200, retry.text
    assert retry.json()["detail"]["code"] == first.json()["detail"]["code"]
    # 完成重试也要验证所有权，不能仅凭知道 upload_id 读出别人的取件码。
    _, other_headers = await create_delivery(delivery_env)
    completed_path = f"/chunk/upload/complete/{upload_id}" if kind == "chunk" else f"/presign/upload/confirm/{upload_id}"
    stolen = await client.post(completed_path, headers=other_headers,
                               json={"expire_value": 1, "expire_style": "day"})
    assert stolen.status_code == 404, stolen.text
    assert first.json()["detail"]["code"] not in stolen.text
    record = await DeliveryCode.get(id=code_id)
    assert (record.used_count, record.reserved_count, record.enabled) == (1, 0, False)
    assert await FileCodes.filter(delivery_id=code_id).count() == 1
    assert await StorageReservation.all().count() == 0
    assert (await send_text(client, headers)).status_code == 403
    listed = await client.get("/admin/file/list/", headers=admin, params={"delivery_id": code_id})
    assert listed.status_code == 200, listed.text
    assert (await client.delete(f"/admin/delivery/codes/{code_id}", headers=admin)).status_code == 200
    assert (await DeliveryCode.get(id=code_id)).deleted is True
    selected = await client.post("/share/select/", json={"code": first.json()["detail"]["code"]})
    downloaded = await client.get(selected.json()["detail"]["download_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"abcdefgh"


async def test_declared_size_mismatch_does_not_consume_success_count(delivery_env):
    """代理上传不能少报字节绕过容量限制，失败后允许按声明大小重试。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env)
    started = await init_upload(client, headers, "presign")
    url = started.json()["detail"]["upload_url"]
    rejected = await client.put(url, headers=headers, files={"file": ("parcel.bin", b"too large")})
    assert rejected.status_code == 400, rejected.text
    assert (await DeliveryCode.get(id=created["item"]["id"])).used_count == 0
    assert await FileCodes.all().count() == 0
    accepted = await client.put(url, headers=headers, files={"file": ("parcel.bin", b"abcdefgh")})
    assert accepted.status_code == 200, accepted.text


async def test_failed_cleanup_keeps_bytes_reserved_until_retry(delivery_env, monkeypatch):
    """磁盘清理失败只归还次数，残留继续占容量；重试成功才释放空间。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env)
    await set_config(storage_limit=8)
    started = await init_upload(client, headers, "presign")
    upload_id = started.json()["detail"]["upload_id"]
    reservation = await StorageReservation.get(token=upload_id)
    storage = SystemFileStorage()
    path = storage._resolve_safe_path(f"{reservation.file_path}/{reservation.stored_name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abcdefgh")

    async def disk_failure(self, file):
        raise OSError("模拟暂时无法删除物理文件")

    # 仅替换物理删除这一外部失败点，数据库、次数和容量逻辑保持真实。
    with monkeypatch.context() as patch:
        patch.setattr(SystemFileStorage, "delete_file", disk_failure)
        cancelled = await client.delete(f"/presign/upload/{upload_id}", headers=headers)
        assert cancelled.status_code == 200, cancelled.text
        assert (await StorageReservation.get(token=upload_id)).status == "cleanup"
        assert (await DeliveryCode.get(id=created["item"]["id"])).reserved_count == 0
        assert (await get_storage_usage())["reserved"] == 8
        assert path.exists()
    await cleanup_once()
    assert not path.exists()
    assert (await get_storage_usage())["reserved"] == 0
    assert not await StorageReservation.filter(token=upload_id).exists()


async def test_list_paginates_filters_and_only_explicit_secret_endpoint_reveals_code(delivery_env):
    """分页总数与内容一致，普通列表不泄露口令，标签按完整值匹配。"""
    client, admin = delivery_env
    first, _ = await create_delivery(delivery_env, name="项目A", tags=["验收"], note="待验收")
    second, _ = await create_delivery(delivery_env, name="项目B", tags=["验收扩展"])
    listed = await client.get("/admin/delivery/codes", headers=admin,
                              params={"page_size": 1, "sort_by": "name", "sort_order": "asc"})
    detail = listed.json()["detail"]
    assert detail["total"] == 2
    assert [item["name"] for item in detail["items"]] == ["项目A"]
    assert first["code"] not in listed.text and second["code"] not in listed.text
    second_page = await client.get("/admin/delivery/codes", headers=admin,
                                   params={"page_size": 1, "page": 2, "sort_by": "name", "sort_order": "asc"})
    assert [item["name"] for item in second_page.json()["detail"]["items"]] == ["项目B"]
    tagged = await client.get("/admin/delivery/codes", headers=admin, params={"tag": "验收"})
    assert tagged.json()["detail"]["total"] == 1
    assert tagged.json()["detail"]["items"][0]["id"] == first["item"]["id"]
    secret = await client.get(f"/admin/delivery/codes/{first['item']['id']}/secret", headers=admin)
    assert secret.json()["detail"]["code"] == first["code"]
    assert secret.headers["cache-control"] == "no-store"


async def test_stale_upload_releases_slot_and_session(delivery_env):
    """中断的上传到期后必须回收，不依赖访客主动取消。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env, max_uploads=1)
    started = await init_upload(client, headers)
    upload_id = started.json()["detail"]["upload_id"]
    await StorageReservation.filter(token=upload_id).update(expires_at=await get_now() - timedelta(seconds=1))
    await cleanup_once()
    assert (await DeliveryCode.get(id=created["item"]["id"])).reserved_count == 0
    assert not await UploadChunk.filter(upload_id=upload_id).exists()
    assert not await StorageReservation.filter(token=upload_id).exists()
    assert (await init_upload(client, headers)).status_code == 200


@pytest.mark.parametrize("prefix", ["/chunk", "/presign", "/api/presign"])
async def test_missing_delivery_session_returns_json_404_including_legacy_alias(delivery_env, prefix):
    """丢失的寄件会话及旧 API 别名也必须返回 JSON 404，不得落入主题首页。"""
    client, _ = delivery_env
    _, headers = await create_delivery(delivery_env)
    response = await client.get(f"{prefix}/upload/status/d_missing", headers=headers)
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "上传会话不存在"


async def test_reenable_does_not_revive_old_tokens(delivery_env):
    """重新启用只能签发新凭证，不能复活停用前已撤销的 token。"""
    client, admin = delivery_env
    created, old_headers = await create_delivery(delivery_env)
    code_id = created["item"]["id"]
    for enabled in (False, True):
        response = await client.patch(f"/admin/delivery/codes/{code_id}", headers=admin, json={"enabled": enabled})
        assert response.status_code == 200, response.text
    assert (await send_text(client, old_headers)).status_code == 401
    verified = await client.post("/api/delivery/verify", json={"code": created["code"]})
    assert verified.status_code == 200, verified.text
    headers = {"Authorization": f"Bearer {verified.json()['detail']['token']}"}
    assert (await send_text(client, headers)).status_code == 200


async def test_text_quota_counts_utf8_bytes_and_failed_attempt_does_not_spend_slot(delivery_env):
    """中文按 UTF-8 字节计费；容量拒绝后，最后一次寄件机会仍可使用。"""
    client, _ = delivery_env
    created, headers = await create_delivery(delivery_env, max_uploads=1)
    await set_config(storage_limit=5)
    rejected = await send_text(client, headers, "中文")
    assert rejected.status_code == 507, rejected.text
    record = await DeliveryCode.get(id=created["item"]["id"])
    assert (record.used_count, record.reserved_count) == (0, 0)
    await set_config(storage_limit=6)
    assert (await send_text(client, headers, "中文")).status_code == 200
    usage = await get_storage_usage()
    assert (usage["used"], usage["reserved"]) == (6, 0)


async def test_delivery_cannot_override_system_storage_or_file_type_limit(delivery_env):
    """授权不提供单独存储配置，受限寄件同样必须遵守站点文件类型白名单。"""
    client, admin = delivery_env
    created, headers = await create_delivery(delivery_env)
    response = await client.put(f"/admin/delivery/codes/{created['item']['id']}", headers=admin,
                                json={"storage_type": "s3", "storage_path": "another-box"})
    assert response.status_code == 422, response.text
    await set_config(allowed_file_types=["txt"])
    rejected = await init_upload(client, headers, "presign", name="parcel.exe")
    assert rejected.status_code == 403, rejected.text
    assert (await DeliveryCode.get(id=created["item"]["id"])).reserved_count == 0
    assert await StorageReservation.all().count() == 0
