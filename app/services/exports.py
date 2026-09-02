"""记录导出(借鉴三个同类项目都把结果推到飞书多维表格的做法,做成通用输出通道):

画像 outputs.exports 声明目标与字段映射,能力 exports.run 把已发布记录按映射写出去。
目前实现 feishu_bitable(凭证在设置页 feishu_app_id/secret);字段映射的值可以是角色名
(subject/dim1/…)、payload 路径(a.b)或 record.<列>(event_id/status)。HTTP 层可注入(便于测试)。
"""
from __future__ import annotations

from app.config import settings
from app.services import need_ctx
from app.services.need_ctx import ROLE_COLUMNS

_FEISHU = "https://open.feishu.cn/open-apis"


class FeishuBitable:
    def __init__(self, app_id: str | None = None, app_secret: str | None = None, http=None):
        self.app_id = app_id or getattr(settings, "feishu_app_id", "")
        self.app_secret = app_secret or getattr(settings, "feishu_app_secret", "")
        self._http = http
        self._token = None

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=20)
        return self._http

    def token(self) -> str:
        if self._token:
            return self._token
        if not (self.app_id and self.app_secret):
            raise RuntimeError("未配置飞书应用凭证(feishu_app_id / feishu_app_secret)")
        r = self._client().post(f"{_FEISHU}/auth/v3/tenant_access_token/internal",
                                json={"app_id": self.app_id, "app_secret": self.app_secret})
        data = r.json()
        if data.get("code") not in (0, None) or not data.get("tenant_access_token"):
            raise RuntimeError(f"飞书鉴权失败:{data}")
        self._token = data["tenant_access_token"]
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    def list_records(self, app_token: str, table_id: str, page_size: int = 500) -> list[dict]:
        out, page_token = [], None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            r = self._client().get(f"{_FEISHU}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                                   headers=self._headers(), params=params)
            data = r.json().get("data") or {}
            out += data.get("items") or []
            if not data.get("has_more") or not data.get("page_token"):
                return out
            page_token = data["page_token"]

    def batch_create(self, app_token: str, table_id: str, records: list[dict]) -> int:
        n = 0
        for i in range(0, len(records), 100):
            chunk = [{"fields": f} for f in records[i:i + 100]]
            r = self._client().post(f"{_FEISHU}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
                                    headers=self._headers(), json={"records": chunk})
            data = r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"飞书写入失败:{str(data)[:200]}")
            n += len(((data.get("data") or {}).get("records")) or chunk)
        return n


def _value(ev, spec: str, ctx):
    spec = str(spec)
    p = ev.payload or {}
    if spec.startswith("record."):
        return getattr(ev, spec[7:], None)
    if spec in ROLE_COLUMNS or spec == "title":
        v = ctx.get_role(p, spec)
        return v if v is not None else getattr(ev, ROLE_COLUMNS.get(spec, ""), None)
    return need_ctx.dget(p, spec)


def render_fields(ev, field_map: dict, ctx) -> dict:
    out = {}
    for feishu_field, spec in (field_map or {}).items():
        v = _value(ev, spec, ctx)
        if isinstance(v, list):
            v = [str(x) for x in v if x not in (None, "")]
        elif isinstance(v, dict):
            v = v.get("value") or v.get("name") or v.get("level") or str(v)
        if v not in (None, "", []):
            out[str(feishu_field)] = v
    return out


def run(db, ctx, name: str | None = None, statuses=("published", "monitoring"), dry_run: bool = False,
        http=None) -> dict:
    """按画像 outputs.exports 执行导出。name 指定其中一个;dry_run 只渲染不写。"""
    from app.models import Event
    exports = [e for e in (ctx.outputs.get("exports") or []) if isinstance(e, dict)]
    if name:
        exports = [e for e in exports if e.get("name") == name]
    if not exports:
        return {"ok": False, "note": "画像未声明 outputs.exports(或名字不匹配)", "exports": []}
    evs = db.query(Event).filter(Event.need_id == ctx.id, Event.status.in_(list(statuses))).all()
    results = []
    for ex in exports:
        kind = ex.get("kind") or "feishu_bitable"
        key_field = ex.get("key_field") or "记录号"
        fmap = dict(ex.get("field_map") or {})
        fmap.setdefault(key_field, "record.event_id")
        rows = [render_fields(ev, fmap, ctx) for ev in evs]
        item = {"name": ex.get("name") or kind, "kind": kind, "records": len(rows), "written": 0, "skipped": 0}
        if dry_run:
            item["preview"] = rows[:3]
            results.append(item)
            continue
        if kind == "feishu_bitable":
            client = FeishuBitable(http=http)
            existing = {str((r.get("fields") or {}).get(key_field)) for r in
                        client.list_records(ex["app_token"], ex["table_id"])}
            new = [r for r in rows if str(r.get(key_field)) not in existing]
            item["skipped"] = len(rows) - len(new)
            item["written"] = client.batch_create(ex["app_token"], ex["table_id"], new) if new else 0
        else:
            item["error"] = f"不支持的导出类型 {kind}"
        results.append(item)
    return {"ok": all("error" not in r for r in results), "exports": results}
