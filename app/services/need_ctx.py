"""需求上下文(NeedContext):把需求画像解析成带缺省值的参数对象,是引擎**唯一**的取参入口。

为什么要有这一层:画像 YAML 早就声明了六要素,但引擎里粗筛提示词、范畴闸门、金额守卫、
去重键、查询列、覆盖维度、找源配方、回访触发、线索评分、报表口径、界面文案……全部写死成
安全事件库。结果"第二个需求"只是注册了、数据隔离了,从没真正跑过引擎。

约定(详见 design/platform/02):
- 每个维度都有缺省值;缺省 = 平台中性行为(不是某个行业的行为)。行业个性只存在于画像里。
- 引擎模块只从这里取参,禁止各自去读 YAML;取参统一走 `need_ctx.get(db, need_id)`。
- 画像可增量升级:老画像缺键照常跑(用缺省),新键随时补。
- `field_roles` 把"payload 里的路径"映射到"角色";查询列/报表/界面只认角色,不认行业字段名。
"""
import copy
import glob
import hashlib
import json
import re
import threading
from pathlib import Path

import yaml

from app.config import BASE_DIR, settings

# ---------------- 角色 → 物理查询列(物理列名沿用,语义由画像决定;见 01 §1.4) ----------------
ROLE_COLUMNS = {
    "dim1": "industry_l1", "dim2": "industry_l2", "region": "province", "region2": "city",
    "subject": "org_name", "subject_key": "org_uscc", "subject_type": "org_type",
    "subject_size": "org_size", "grade": "severity", "tags_a": "attack_types",
    "tags_b": "consequences", "occurred_date": "occurred_date", "disclosed_date": "disclosed_date",
    "record_type": "record_type",
}
STANDARD_ROLES = ["title", *ROLE_COLUMNS.keys()]
# 物理列长度(PG 会强校验;SQLite 不管),写入前截断
ROLE_COLUMN_LIMITS = {"industry_l1": 32, "industry_l2": 64, "province": 32, "city": 64, "org_name": 256,
                      "org_uscc": 32, "org_type": 32, "org_size": 8, "severity": 8, "record_type": 16}

# 通用的"确认/声称"语境词:属于事实核实型质量模型的语言层规律,不是某个行业的词
_GENERIC_CLAIMED = "要求|索赔|拟|预计|或将|据传|传闻|主张|声称|估计|约合"
_GENERIC_CONFIRMED = "判决|裁定|决定书|行政处罚|公告(?:披露|确认)|年报(?:披露|确认)|已支付|已缴纳|正式发布|正式生效"

DEFAULTS: dict = {
    "record": {
        "id_prefix": "REC",
        "field_roles": {r: r for r in STANDARD_ROLES},
        "role_labels": {"title": "标题", "subject": "主体", "dim1": "分类", "dim2": "子类",
                        "region": "地区", "grade": "等级", "tags_a": "标签", "tags_b": "标签2",
                        "occurred_date": "发生日", "disclosed_date": "披露日", "record_type": "类型"},
        "record_types": {
            "values": ["单一记录", "汇总情报", "不该入库"], "default": "单一记录",
            "out_of_scope": "不该入库",
            "advisory": {"value": "汇总情报",
                         "hints": ["通报", "情况通报", "汇总", "盘点", "统计", "月报", "周报",
                                   "季度", "上半年", "下半年", "综述", "专报"],
                         "subject_blank_values": ["", "未披露", "未知", "不明", "无", "多家", "多个", "若干"]},
        },
        "grade_order": [],
        # 系统信封字段:记录号写到哪个键、系统状态(draft/published)写到哪个键(空串=不注入,
        # 例如文档型画像自己有业务 status 枚举时)
        "envelope": {"id_field": "event_id", "status_field": "status", "review_field": "review"},
        "completeness_weights": {},          # 空 → 按 Schema required 字段等权
        "dedup": {"subject_roles": ["subject_key", "subject+region"], "type_role": "tags_a",
                  "date_role": "occurred_date", "window_days": None},
        "confidence_by_credibility": {"S1": "已证实", "S2": "多源印证", "S3": "单源待证", "S4": "单源待证"},
    },
    "dictionaries": {"relevance_term_fields": ["event_terms", "consequence_terms"]},
    "coverage": {"dimension_role": "dim1", "dictionary_key": "industries", "scope_kind": None,
                 "query_templates": [], "short_names": {}, "placeholders": ["其他", "其它", "未分类", "未知", "词表外"],
                 "window_days": None, "min_records": None},
    "sources": {
        "discovery_file": None,               # None → 由关键词模块按 scope 生成配方
        "column_discovery": {"hint_words": [], "stop_words": [
            "招聘", "关于我们", "联系", "网站地图", "版权", "登录", "注册", "English", "简介",
            "机构设置", "领导", "党建", "会议", "视频", "图片", "专题", "首页", "邮箱", "服务"],
            "path_hints": []},
        "prospect": {"selftest_query": "", "probe_prompt": "", "harvest_prompt": ""},
        "region_policy": None,               # None → 由 need.regions/languages 推导
        "skip_hosts_extra": [],
        "grading": None,                     # None → discovery_file.grading
        "discovery_scoring": None,           # None → discovery_file.scoring
        # 真实性验证:空 → 平台常量(.gov.cn 等官方后缀、.org.cn 等机构后缀、内部/密级标记)
        "verification": {"official_suffixes": [], "official_domains": [], "medium_suffixes": [], "sensitive_markers": []},
    },
    "quality": {
        "screen": {"goal": "", "include_rules": [], "exclude_rules": [], "reminder": ""},
        "extract_rules": [],
        "scope_guard": {"exclude_patterns": [], "include_override_patterns": [], "out_of_scope_reason": ""},
        # 记录关系抽取规则(废止/替代/修订/依据):空 → 平台缺省正则;append_default=False 则只用画像的
        "relations": {"patterns": [], "append_default": True},
        "assertions": {
            "tristate_fields": None,         # None → quality.assertion_tristate_fields
            "channels": {"claimed": "claimed_cny", "estimated": "estimated_cny", "confirmed": "confirmed_cny"},
            "labels": {},                    # 三态字段显示名
            "claimed_markers": _GENERIC_CLAIMED,
            "confirmed_markers": _GENERIC_CONFIRMED,
            "isolation": [],
            "paid_check": None,
        },
    },
    "update": {"followup_schedule": [30, 90, 180, 365], "followup_triggers": [],
               "followup_search": {"subject_role": "subject", "query_suffixes": [], "link_templates": {}}},
    "outputs": {
        "leads_engine": {"enabled": False, "mapping_file": None, "write_back_field": None, "subject_role": "subject",
                         "grade_weights": {}, "size_weights": {},
                         "window_stages": [{"name": "应急期", "max_days": 30, "weight": 1.0},
                                           {"name": "整改期", "max_days": 180, "weight": 0.9},
                                           {"name": "预算期", "max_days": 540, "weight": 0.6},
                                           {"name": "已过窗", "max_days": None, "weight": 0.2}],
                         "match_dims": {}, "talk_track": {"facts": []}},
        "reports_engine": {"crosstab": {"row_role": "dim1", "col_roles": ["tags_a", "tags_b"]},
                           "amount_sum": {"fields": None, "group_role": "dim1", "default_scope": "confirmed"},
                           "status_count": None, "missing_field": None},
        "digest": {"title": "", "group_roles": ["dim1", "grade"], "rank_role": "grade", "advisory_split": True},
        # 组件:通知渠道(空 → 用运行时设置里配齐的渠道)、导出目标(空 → 不导出)、评分卡权重(空 → 0.4/0.3/0.2/0.1)
        "notify": {"channels": []},
        "exports": [],
        "quality_scorecard": {"weights": {}},
    },
    "ui": {
        "record_label": "记录",
        "tabs": {"dashboard": {"label": "仪表盘", "enabled": True}, "events": {"label": "记录", "enabled": True},
                 "review": {"label": "复核台", "enabled": True}, "followups": {"label": "回访", "enabled": True},
                 "leads": {"label": "线索", "enabled": False}, "sources": {"label": "数据源", "enabled": True},
                 "crawl": {"label": "采集", "enabled": True}, "actions": {"label": "系统动作", "enabled": True},
                 "digest": {"label": "日报", "enabled": True}, "settings": {"label": "设置", "enabled": True}},
        "list_columns": [{"role": "title"}, {"role": "record_type"}, {"role": "dim1"}, {"role": "grade"},
                         {"role": "tags_a"}, {"role": "status", "label": "状态"},
                         {"role": "completeness", "label": "完备度"}, {"role": "disclosed_date"}],
        "filters": [{"role": "status", "label": "状态"}, {"role": "grade"}],
        "detail_sections": [{"kind": "kv", "roles": ["subject", "dim1", "region", "grade"]},
                            {"kind": "tags", "roles": ["tags_a", "tags_b"]},
                            {"kind": "tristate"}, {"kind": "sources"}],
        "dashboard_tiles": [],
    },
    "demo": {"samples": []},
    "mock": {"screen_keywords": [], "extract_rules": []},
    # ---- 范围限定(scope):需求"要什么"的五个维度。每项可写 值 或 {value, terms|aliases}。
    # 它同时喂给:关键词生成(词组)、粗筛提示词(范围说明)、范畴闸门(require_mention)、覆盖度(维度取值)、找源配方。
    "scope": {"regions": [], "industries": [], "topics": [], "entities": [], "doc_types": [],
              "require_mention": [],        # 例 ["entities"]:标题/正文必须提到该维度至少一个词,否则范畴外
              "time_window_days": None},    # None → 运行时设置 collect_recency_days
    # ---- 关键词生成与组合(keywords 能力模块)
    "keywords": {"groups": {},               # 静态词组 {group: [terms]},与 scope 派生词组合并
                 "compose": [],              # 组合配方 [{groups:[a,b], template:"{0} {1}", limits:[n,m]}];空 → 平台缺省配方
                 "expand_with_llm": False, "expand_per_term": 3,
                 "time_filters": [], "negative_terms": [],
                 "query_budget_per_source_daily": 200, "max_pages_per_query": 3,
                 "auto_generate": True},     # 画像没给 discovery_terms_file 时装载即自动生成矩阵
    # ---- 处理流水线组合:阶段可增删排序;extract_mode=light 时不需要 Schema 文件(按角色生成轻量 Schema)
    "pipeline": {"stages": ["screen", "verify", "extract", "scope_gate", "content_check", "dedup_record", "draft"],
                 "extract_mode": "schema", "light_fields": []},
}
SCOPE_KINDS = ("regions", "industries", "topics", "entities", "doc_types")
SCOPE_GROUP = {"regions": "region_terms", "industries": "industry_terms", "topics": "topic_terms",
               "entities": "entity_terms", "doc_types": "doctype_terms"}
SCOPE_LABEL = {"regions": "地域", "industries": "行业", "topics": "主题", "entities": "主体", "doc_types": "文种"}
_LIGHT_ROLES = {"title": "title", "subject": "subject", "dim1": "category", "region": "region",
                "tags_a": "tags", "occurred_date": "published_date", "record_type": "record_type"}

_FOREIGN_TLDS = [".jp", ".kr", ".ca", ".us", ".uk", ".de", ".fr", ".au", ".in", ".ru", ".br", ".sg",
                 ".my", ".th", ".vn", ".id", ".ph", ".nz", ".it", ".es", ".nl", ".se", ".ch", ".il",
                 ".za", ".pl", ".tr", ".mx", ".ar"]
_CN_TLDS = [".cn", ".com.cn", ".gov.cn", ".org.cn", ".net.cn", ".edu.cn", ".中国"]
_CN_REGION_WORDS = ("中国大陆", "中国", "china", "cn", "国内")


_EXPLICIT_NULL_KEYS = ("record.dedup.type_role", "outputs.reports_engine.amount_sum",
                       "outputs.reports_engine.status_count", "outputs.reports_engine.missing_field",
                       "quality.assertions.paid_check")


def _has_explicit_null(cfg: dict, path: str) -> bool:
    cur = cfg
    parts = path.split(".")
    for k in parts[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return isinstance(cur, dict) and parts[-1] in cur and cur[parts[-1]] is None


def _set_path(obj: dict, path: str, value):
    cur = obj
    parts = path.split(".")
    for k in parts[:-1]:
        cur = cur.setdefault(k, {})
    cur[parts[-1]] = value


def _deep_merge(base, override):
    """字典递归合并;列表/标量以 override 为准;override 里的 None 不覆盖(表示"用缺省")。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            if v is None:
                continue
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def dget(obj, dotpath: str):
    """点路径取值:支持 a.b、a[0]、a.b[0].c;任一段缺失返回 None。"""
    if obj is None or not dotpath:
        return None
    cur = obj
    for m in _PATH_TOKEN.finditer(dotpath):
        key, idx = m.group(1), m.group(2)
        if key is not None:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        else:
            if not isinstance(cur, list) or int(idx) >= len(cur):
                return None
            cur = cur[int(idx)]
        if cur is None:
            return None
    return cur


class NeedContext:
    def __init__(self, need_id: str, cfg: dict | None):
        cfg = cfg or {}
        self.id = need_id
        self.raw = cfg
        self.name = (cfg.get("need") or {}).get("name") or need_id
        self.need = cfg.get("need") or {}
        m = _deep_merge(DEFAULTS, {k: cfg.get(k) for k in DEFAULTS if k in cfg})
        # 显式 null 在这些键上是有含义的("不用这一项"),不能被缺省值覆盖
        for path in _EXPLICIT_NULL_KEYS:
            if _has_explicit_null(cfg, path):
                _set_path(m, path, None)
        self.record = m["record"]
        self.dictionaries_cfg = _deep_merge(DEFAULTS["dictionaries"], cfg.get("dictionaries") or {})
        self.coverage = m["coverage"]
        self.sources_cfg = m["sources"]
        self.quality = m["quality"]
        self.update = m["update"]
        self.outputs = m["outputs"]
        self.ui = m["ui"]
        self.demo = m["demo"]
        self.mock = m["mock"]
        self.scope_cfg = m["scope"]
        self.keywords_cfg = m["keywords"]
        self.pipeline_cfg = m["pipeline"]
        # 轻量抽取模式:角色缺省对齐轻量 Schema(title/subject/category/region/tags/published_date),
        # 画像声明的角色覆盖缺省(如把 dim2 指到追加字段 industry)
        if self.extract_mode == "light":
            declared = dict((cfg.get("record") or {}).get("field_roles") or {})
            self.record["field_roles"] = {**self.record["field_roles"], **_LIGHT_ROLES, **declared}
        # 兼容旧键
        q = cfg.get("quality") or {}
        if not self.quality["screen"].get("goal") and q.get("screen_prompt"):
            self.quality["screen"]["goal"] = str(q["screen_prompt"]).strip()
        if self.quality["assertions"].get("tristate_fields") is None:
            self.quality["assertions"]["tristate_fields"] = list(q.get("assertion_tristate_fields") or [])
        self.double_review_fields = list(q.get("double_review_fields") or self.quality["assertions"]["tristate_fields"])
        u = cfg.get("update") or {}
        if u.get("followup_schedule"):
            self.update["followup_schedule"] = list(u["followup_schedule"])
        self.lagged_fields = list(u.get("lagged_fields") or [])
        self.watch_kinds = list(u.get("watch_kinds") or [])
        self._discovery_cache: dict | None = None

    # 画像没声明的窗口参数按运行时设置(设置页可改,取值时才读,不在构造时固化)
    @property
    def coverage_window_days(self) -> int:
        v = self.coverage.get("window_days")
        return int(v) if v else int(getattr(settings, "coverage_window_days", 30))

    @property
    def coverage_min_records(self) -> int:
        v = self.coverage.get("min_records")
        return int(v) if v else int(getattr(settings, "coverage_min_events", 1))

    @property
    def dedup_window_days(self) -> int:
        v = self.record["dedup"].get("window_days")
        return int(v) if v else int(getattr(settings, "fingerprint_window_days", 14))

    @property
    def subject_blank_values(self) -> set[str]:
        adv = self.record_types.get("advisory") or {}
        return {str(x) for x in (adv.get("subject_blank_values") or [""])} | {""}

    # ---------------- 记录 / 字段角色 ----------------
    @property
    def archetype(self) -> str:
        return ((self.raw.get("record_schemas") or [{}])[0].get("archetype")) or "事件型"

    @property
    def schema_file(self) -> Path | None:
        """记录 Schema 文件;轻量模式(或画像未给文件)返回 None,此时用 record_schema() 生成。"""
        f = (self.raw.get("record_schemas") or [{}])[0].get("file")
        if f:
            return self.path(f)
        return None if self.extract_mode == "light" or not self.raw.get("record_schemas") else None

    @property
    def extract_mode(self) -> str:
        return str(self.pipeline_cfg.get("extract_mode") or "schema")

    @property
    def pipeline_stages(self) -> list[str]:
        return [str(x) for x in (self.pipeline_cfg.get("stages") or DEFAULTS["pipeline"]["stages"])]

    def light_schema(self) -> dict:
        """按角色生成的轻量记录 Schema:标题/摘要/主体/分类/地域/标签/日期 + 画像追加字段。"""
        rt = self.record_types
        props = {
            "title": {"type": "string"}, "summary": {"type": "string"},
            "subject": {"type": "string", "description": "信息涉及的主体(机构/企业/部门),没有填『未披露』"},
            "category": {"type": "string", "description": "分类/主题"},
            "region": {"type": "string", "description": "涉及地域,没有填『未披露』"},
            "published_date": {"type": "string", "description": "YYYY-MM-DD;不明填『未披露』"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "record_type": {"enum": list(rt.get("values") or ["单一记录", "汇总情报", "不该入库"])},
            "sources": {"type": "array", "items": {"type": "object"}},
        }
        for f in self.pipeline_cfg.get("light_fields") or []:
            if isinstance(f, dict) and f.get("name"):
                props[str(f["name"])] = {"type": f.get("type") or "string", "description": f.get("desc") or ""}
            elif isinstance(f, str):
                props[f] = {"type": "string"}
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                "title": f"{self.name} 轻量记录", "required": ["title", "summary"], "properties": props}

    def record_schema(self) -> dict:
        """记录 Schema(dict):有文件读文件,否则按角色生成轻量 Schema。"""
        f = self.schema_file
        if f and f.exists():
            return load_schema_file(f)
        return self.light_schema()

    # ---------------- 范围限定 ----------------
    @property
    def scope(self) -> dict:
        return self.scope_cfg

    def scope_items(self, kind: str) -> list[dict]:
        """某维度的条目,统一成 [{value, terms:[value + 同义/别名]}]。"""
        out = []
        for x in self.scope_cfg.get(kind) or []:
            if isinstance(x, dict):
                v = str(x.get("value") or x.get("name") or "").strip()
                if not v:
                    continue
                extra = [str(t).strip() for t in (x.get("terms") or x.get("aliases") or []) if str(t).strip()]
                out.append({"value": v, "terms": [v] + [t for t in extra if t != v]})
            elif x not in (None, ""):
                out.append({"value": str(x).strip(), "terms": [str(x).strip()]})
        return out

    def scope_values(self, kind: str) -> list[str]:
        return [x["value"] for x in self.scope_items(kind)]

    def scope_terms(self, kind: str) -> list[str]:
        seen, out = set(), []
        for x in self.scope_items(kind):
            for t in x["terms"]:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    @property
    def require_mention(self) -> list[str]:
        return [str(k) for k in (self.scope_cfg.get("require_mention") or []) if str(k) in SCOPE_KINDS]

    @property
    def time_window_days(self) -> int:
        v = self.scope_cfg.get("time_window_days")
        return int(v) if v else int(getattr(settings, "collect_recency_days", 0) or 0)

    def scope_summary(self) -> list[str]:
        """给提示词/界面用的范围说明行。"""
        lines = []
        for kind in SCOPE_KINDS:
            items = self.scope_items(kind)
            if not items:
                continue
            parts = []
            for it in items:
                alias = [t for t in it["terms"] if t != it["value"]]
                parts.append(it["value"] + (f"(含 {'/'.join(alias)})" if alias else ""))
            lines.append(f"{SCOPE_LABEL[kind]}:" + "、".join(parts))
        return lines

    @property
    def min_publish_fields(self) -> list[str]:
        return list((self.raw.get("record_schemas") or [{}])[0].get("min_publish_fields") or [])

    @property
    def id_prefix(self) -> str:
        return str(self.record.get("id_prefix") or "REC")

    @property
    def field_roles(self) -> dict:
        return self.record["field_roles"]

    @property
    def envelope(self) -> dict:
        e = dict(self.record.get("envelope") or {})
        return {"id_field": e.get("id_field", "event_id"), "status_field": e.get("status_field", "status"),
                "review_field": e.get("review_field", "review")}

    @property
    def envelope_fields(self) -> set[str]:
        """草稿阶段不由模型产出、发布时由系统注入的键。"""
        e = self.envelope
        return {x for x in (e["id_field"], e["status_field"], e["review_field"]) if x} | {
            "event_id", "status", "review", "confidence_overall", "completeness_score", "change_log", "sources"}

    def role_paths(self, role: str) -> list[str]:
        """角色对应的 payload 路径(可声明多个别名路径,按序取第一个非空)。"""
        p = self.field_roles.get(role)
        if p is None or p == "":
            return []
        return [str(x) for x in p if x] if isinstance(p, list) else [str(p)]

    def role_path(self, role: str) -> str | None:
        ps = self.role_paths(role)
        return ps[0] if ps else None

    def role_label(self, role: str) -> str:
        return (self.record.get("role_labels") or {}).get(role) or role

    def get_role(self, payload: dict, role: str):
        """按角色取值。路径指向对象内子键(如 severity.level)而模型给了标量(severity:"重大")时退回父级标量。"""
        if not isinstance(payload, dict):
            return None
        paths = self.role_paths(role)
        for p in paths:
            v = dget(payload, p)
            if v is not None:
                return v
        for p in paths:
            if "." in p:
                parent = dget(payload, p.rsplit(".", 1)[0])
                if isinstance(parent, (str, int, float)) and not isinstance(parent, bool):
                    return parent
        return None

    def role_column(self, role: str) -> str | None:
        return ROLE_COLUMNS.get(role)

    @property
    def record_types(self) -> dict:
        return self.record["record_types"]

    @property
    def grade_order(self) -> list[str]:
        return list(self.record.get("grade_order") or [])

    def grade_rank(self, value) -> int:
        order = self.grade_order
        if not order or value not in order:
            return 0
        return len(order) - order.index(value)

    @property
    def completeness_weights(self) -> dict:
        return dict(self.record.get("completeness_weights") or {})

    @property
    def dedup(self) -> dict:
        return self.record["dedup"]

    @property
    def confidence_by_credibility(self) -> dict:
        return self.record["confidence_by_credibility"]

    # ---------------- 词表 / 文件 ----------------
    def path(self, rel: str | Path | None) -> Path | None:
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def dictionaries_file(self) -> Path | None:
        return self.path((self.raw.get("dictionaries") or {}).get("file"))

    @property
    def discovery_terms_file(self) -> Path | None:
        return self.path((self.raw.get("dictionaries") or {}).get("discovery_terms_file"))

    @property
    def relevance_term_fields(self) -> list[str]:
        return list(self.dictionaries_cfg.get("relevance_term_fields") or [])

    @property
    def seed_file(self) -> Path | None:
        return self.path((self.raw.get("sources") or {}).get("seed_file"))

    @property
    def discovery_file(self) -> Path | None:
        return self.path(self.sources_cfg.get("discovery_file"))

    def discovery_yaml(self) -> dict:
        """找源配方 / 候选评分 / 定级规则文件(画像 sources.discovery_file)。没给或读不到 → {};
        此时找源词与配方由关键词模块按 scope 生成(keywords.search_queries_for / recipes_for),
        契约层不反向依赖能力模块。"""
        if self._discovery_cache is None:
            data = {}
            f = self.discovery_file
            if f is not None:
                try:
                    with open(f, encoding="utf-8") as fh:
                        data = yaml.safe_load(fh) or {}
                except (OSError, yaml.YAMLError):
                    data = {}
            self._discovery_cache = data
        return self._discovery_cache

    @property
    def has_discovery_file(self) -> bool:
        return bool(self.discovery_yaml())

    def load_dictionaries_file(self) -> dict:
        p = self.dictionaries_file
        if not p:
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return {}

    # ---------------- 来源 / 可信度 ----------------
    @property
    def credibility_levels(self) -> dict:
        return ((self.raw.get("sources") or {}).get("credibility_levels") or {}).get("levels") or {}

    @property
    def confirm_allowed(self) -> list[str]:
        ca = ((self.raw.get("sources") or {}).get("credibility_levels") or {}).get("confirm_allowed")
        return list(ca) if ca else ["S1", "S2"]

    @property
    def grading(self) -> dict:
        g = self.sources_cfg.get("grading")
        return dict(g) if g else dict(self.discovery_yaml().get("grading") or {})

    @property
    def discovery_scoring(self) -> dict:
        s = self.sources_cfg.get("discovery_scoring")
        return dict(s) if s else dict(self.discovery_yaml().get("scoring") or {})

    @property
    def query_recipes(self) -> dict:
        return dict(self.discovery_yaml().get("query_recipes") or {})

    @property
    def source_search_queries(self) -> list[str]:
        return [str(q).strip() for q in (self.discovery_yaml().get("source_search_queries") or []) if str(q).strip()]

    @property
    def column_discovery(self) -> dict:
        return self.sources_cfg["column_discovery"]

    @property
    def selftest_query(self) -> str:
        q = (self.sources_cfg.get("prospect") or {}).get("selftest_query")
        if q:
            return q
        base = self.source_search_queries
        return base[0] if base else self.name

    @property
    def probe_prompt(self) -> str:
        p = (self.sources_cfg.get("prospect") or {}).get("probe_prompt")
        if p:
            return p
        goal = self.quality["screen"].get("goal") or f"与『{self.name}』相关的内容"
        return ("你在评估一个网站/公众号是否值得作为『" + self.name + "』的持续采集源。"
                "依据给出的站点名与最近文章标题样本,判断它是否持续产出这类内容:" + goal + "。\n"
                "注意:持续产出相关一手内容或权威通报的渠道算相关;纯教程、招聘、产品推广、"
                "与主题无关的门户资讯算不相关。\n"
                '只输出 JSON:{"relevance": 0.0~1.0, "reason": "一句话理由"}')

    @property
    def harvest_prompt(self) -> str:
        p = (self.sources_cfg.get("prospect") or {}).get("harvest_prompt")
        if p:
            return p
        return ("你是『" + self.name + "』的检索词工程师。给你一批最近被判定为『相关』的文章标题,"
                "请挑出最适合拿去搜索引擎**找新渠道**的关键词。要求:"
                "①每个词 2-8 个汉字,是完整的说法(专有名词/专项名/动作名),不要半截词或滑窗碎片;"
                "②要有检索区分度,不要『通报』『公司』『近日』这类泛词;③不要人名、地名单独成词;"
                "④只输出 JSON:{\"terms\": [\"词1\", \"词2\"]}")

    @property
    def region_policy(self) -> dict:
        rp = self.sources_cfg.get("region_policy")
        if rp:
            return {"domestic_tlds": list(rp.get("domestic_tlds") or []),
                    "reject_tlds": list(rp.get("reject_tlds") or []),
                    "require_script": rp.get("require_script") or "none"}
        regions = [str(r).lower() for r in (self.need.get("regions") or [])]
        langs = [str(x).lower() for x in (self.need.get("languages") or [])]
        domestic = any(w in r for r in regions for w in _CN_REGION_WORDS)
        return {"domestic_tlds": list(_CN_TLDS) if domestic else [],
                "reject_tlds": list(_FOREIGN_TLDS) if domestic else [],
                "require_script": "cjk" if ("zh" in langs or not langs) and domestic else "none"}

    @property
    def skip_hosts_extra(self) -> set[str]:
        return {str(h).strip().lower() for h in (self.sources_cfg.get("skip_hosts_extra") or []) if str(h).strip()}

    @property
    def reputation_registry(self) -> Path | None:
        return self.path((self.raw.get("sources") or {}).get("reputation_registry"))

    @property
    def repost_detection(self) -> bool:
        return bool((self.raw.get("sources") or {}).get("repost_detection", True))

    # ---------------- 质量 ----------------
    @property
    def screen(self) -> dict:
        return self.quality["screen"]

    @property
    def extract_rules(self) -> list[str]:
        return [str(r) for r in (self.quality.get("extract_rules") or [])]

    @property
    def scope_guard(self) -> dict:
        return self.quality["scope_guard"]

    @property
    def assertions(self) -> dict:
        return self.quality["assertions"]

    @property
    def tristate_fields(self) -> list[str]:
        return list(self.assertions.get("tristate_fields") or [])

    # ---------------- 更新 ----------------
    @property
    def followup_schedule(self) -> list[int]:
        return [int(x) for x in (self.update.get("followup_schedule") or [])]

    @property
    def followup_triggers(self) -> list[dict]:
        return list(self.update.get("followup_triggers") or [])

    @property
    def followup_search(self) -> dict:
        return self.update["followup_search"]

    # ---------------- 输出 ----------------
    @property
    def leads(self) -> dict:
        return self.outputs["leads_engine"]

    @property
    def reports(self) -> dict:
        return self.outputs["reports_engine"]

    @property
    def digest(self) -> dict:
        d = dict(self.outputs["digest"])
        if not d.get("title"):
            d["title"] = f"{self.name}日报"
        return d

    @property
    def demo_samples(self) -> list[dict]:
        return list(self.demo.get("samples") or [])

    def to_ui(self) -> dict:
        """给前端的界面定义:页签/列/筛选/详情/仪表盘 + 角色标签 + 三态字段。"""
        labels = {r: self.role_label(r) for r in STANDARD_ROLES}
        cols = []
        for c in self.ui.get("list_columns") or []:
            cols.append({"role": c["role"], "label": c.get("label") or labels.get(c["role"], c["role"])})
        filters = []
        for f in self.ui.get("filters") or []:
            filters.append({"role": f["role"], "label": f.get("label") or labels.get(f["role"], f["role"]),
                            "values": f.get("values") or (self.grade_order if f["role"] == "grade" else [])})
        return {"id": self.id, "name": self.name, "record_label": self.ui.get("record_label") or "记录",
                "tabs": self.ui.get("tabs"), "list_columns": cols, "filters": filters,
                "detail_sections": self.ui.get("detail_sections"),
                "dashboard_tiles": self.ui.get("dashboard_tiles"),
                "role_labels": labels, "field_roles": self.field_roles,
                "role_columns": ROLE_COLUMNS,
                "tristate_fields": self.tristate_fields,
                "tristate_labels": (self.assertions.get("labels") or {}),
                "channels": dict(self.assertions.get("channels") or {}),
                "envelope": self.envelope,
                "record_types": self.record_types, "grade_order": self.grade_order,
                "confirm_allowed": self.confirm_allowed, "leads_enabled": bool(self.leads.get("enabled")),
                "archetype": self.archetype, "id_prefix": self.id_prefix,
                "extract_mode": self.extract_mode, "scope": self.scope_summary(),
                "pipeline_stages": self.pipeline_stages}


# ---------------- Schema 文件加载(带缓存;契约层自有,不依赖处理层) ----------------

_SCHEMA_CACHE: dict[str, dict] = {}


def load_schema_file(path) -> dict:
    key = str(path)
    if key not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[key] = json.loads(Path(path).read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[key]


# ---------------- 获取与缓存 ----------------

_CACHE: dict[str, tuple[str, NeedContext]] = {}
_LOCK = threading.Lock()


def _cfg_hash(cfg: dict) -> str:
    return hashlib.md5(json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def from_config(cfg: dict) -> NeedContext:
    nid = (cfg.get("need") or {}).get("id") or "unknown"
    h = _cfg_hash(cfg)
    with _LOCK:
        hit = _CACHE.get(nid)
        if hit and hit[0] == h:
            return hit[1]
        ctx = NeedContext(nid, cfg)
        _CACHE[nid] = (h, ctx)
        return ctx


def for_need(need) -> NeedContext:
    """从 NeedProfile ORM 对象取上下文。"""
    return from_config(need.config or {"need": {"id": need.id, "name": need.name}})


def profile_files() -> list[Path]:
    pattern = str(getattr(settings, "need_profile_glob", "") or "config/need_*.yaml")
    files = sorted(Path(p) for p in glob.glob(str(BASE_DIR / pattern)))
    return [f for f in files if "template" not in f.name]


def load_profile_config_file(path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def load_profile_config(need_id: str) -> dict | None:
    """没有 DB 会话时按文件找画像:config/need_<id>.yaml → 任一画像文件里 need.id 匹配 → 任务文件 config/tasks/<id>.yaml 编译。"""
    direct = BASE_DIR / "config" / f"need_{need_id}.yaml"
    candidates = [direct] + [p for p in profile_files() if p != direct]
    for p in candidates:
        try:
            with open(p, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if (cfg.get("need") or {}).get("id") == need_id:
            return cfg
    # 任务模式:config/tasks/<id>.yaml → 编译成画像(tasklib 属底座层,只读任务/参数库文件)
    from app.services import tasklib
    if tasklib.find_task(need_id):
        try:
            return tasklib.compile_task_id(need_id)
        except (KeyError, ValueError, OSError, yaml.YAMLError):
            return None
    return None


def get(db, need_id: str | None = None) -> NeedContext:
    """引擎统一取参入口。有 db 读库里的画像;没有 db(或库里没有)按文件找;都没有给中性缺省。"""
    need_id = need_id or default_need_id()
    cfg = None
    if db is None:
        # 没带会话:优先用本进程最近构造过的同名上下文(流水线/接口刚按库里画像建过),
        # 保证 Mock 模型、守卫缺省等无会话调用看到的和主流程一致
        with _LOCK:
            hit = _CACHE.get(need_id)
        if hit:
            return hit[1]
    if db is not None:
        try:
            from app.models import NeedProfile
            np = db.get(NeedProfile, need_id)
            if np is not None:
                cfg = np.config
        except Exception:  # noqa: BLE001 取不到就退回文件
            cfg = None
    if cfg is None:
        cfg = load_profile_config(need_id)
    if cfg is None and db is None:
        # 没带会话、也没有画像文件(只在库里注册过的需求,如页面/接口创建的):自己开一个短会话查
        try:
            from app.db import SessionLocal
            from app.models import NeedProfile
            s = SessionLocal()
            try:
                np = s.get(NeedProfile, need_id)
                cfg = dict(np.config) if np is not None and np.config else None
            finally:
                s.close()
        except Exception:  # noqa: BLE001 库不可用就退回中性缺省
            cfg = None
    if cfg is None:
        cfg = {"need": {"id": need_id, "name": need_id}}
    return from_config(cfg)


def default_need_id() -> str:
    """平台默认需求:设置项 default_need_id;没配就取第一个画像文件里的 need.id。"""
    v = str(getattr(settings, "default_need_id", "") or "").strip()
    if v:
        return v
    for f in profile_files():
        cfg = load_profile_config_file(f) or {}
        nid = (cfg.get("need") or {}).get("id")
        if nid:
            return str(nid)
    from app.services import tasklib
    for t in tasklib.list_tasks():
        if t.get("id"):
            return str(t["id"])
    return "default"


def file_need_ids() -> list[str]:
    """文件里声明的全部需求 id:画像文件 config/need_*.yaml 的 need.id + 任务文件 config/tasks/*.yaml 的 task.id(去重、保序)。"""
    from app.services import tasklib
    out: list[str] = []
    for f in profile_files():
        nid = ((load_profile_config_file(f) or {}).get("need") or {}).get("id")
        if nid and nid not in out:
            out.append(str(nid))
    for t in tasklib.list_tasks():
        if t.get("id") and t["id"] not in out:
            out.append(str(t["id"]))
    return out


def reset_cache():
    with _LOCK:
        _CACHE.clear()
