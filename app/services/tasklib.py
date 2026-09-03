"""任务模式与参数库(能力层):

- **参数库(preset)**:`config/library/<kind>/<id>.yaml`,一段可复用的画像片段(scope 地域词、记录形态、质量规则、
  节奏、输出…),带 id/kind/name/tags/applies_to/version/provenance;
- **任务(task)**:`config/tasks/<id>.yaml`,"这一次要干什么":task 元信息(目标/状态/节奏/预算)+ `use:`(引用的参数库条目)
  + `overrides:`(本任务特有的画像键);
- **编译**:任务 → 画像(need profile):按 `use` 顺序深合并各条目的 body,再合并 overrides,再把 task.schedule/budget
  映射到画像键;编译产物就是引擎契约(NeedContext),引擎零改动;
- **提炼**:把某个任务/画像的一段(如 scope.regions)存成参数库条目,其它任务 `use` 即可复用。
合并规则:字典递归;两边都是列表 → 追加去重(词表/规则天然可叠加);标量以后者为准。
"""
from __future__ import annotations

import copy
import glob
from datetime import datetime
from pathlib import Path

import yaml

from app.config import BASE_DIR, settings

LIBRARY_DIR = BASE_DIR / "config" / "library"
TASKS_DIR = BASE_DIR / "config" / "tasks"
KINDS = ("scope", "record", "keywords", "sources", "quality", "schedule", "outputs", "ui", "mock", "misc")
# task.schedule.cadence → need.timeliness_sla;其它节奏字段 → 画像键
_CADENCE = {"hourly": "小时级", "daily": "日级", "weekly": "周级", "alert": "告警级",
            "小时级": "小时级", "日级": "日级", "周级": "周级", "告警级": "告警级"}


# ---------------- 合并 ----------------

def merge(base, over):
    """字典递归合并;列表追加去重(以 JSON 序列化比较);标量以 over 为准;over 为 None 不覆盖。"""
    if over is None:
        return copy.deepcopy(base)
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    if isinstance(base, list) and isinstance(over, list):
        import json
        seen = {json.dumps(x, ensure_ascii=False, sort_keys=True, default=str) for x in base}
        out = copy.deepcopy(base)
        for x in over:
            key = json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(copy.deepcopy(x))
        return out
    return copy.deepcopy(over)


# ---------------- 参数库 ----------------

def library_files() -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(LIBRARY_DIR / "*" / "*.yaml")))


def load_preset_file(path: Path) -> dict | None:
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    meta = d.get("preset") or {}
    if not meta.get("id"):
        return None
    meta.setdefault("kind", path.parent.name)
    return {"meta": meta, "body": d.get("body") or {}, "path": str(path)}


def library() -> dict[str, dict]:
    out = {}
    for p in library_files():
        item = load_preset_file(p)
        if item:
            out[item["meta"]["id"]] = item
    return out


def list_presets(kind: str | None = None, tag: str | None = None) -> list[dict]:
    rows = []
    for pid, item in library().items():
        m = item["meta"]
        if kind and m.get("kind") != kind:
            continue
        if tag and tag not in (m.get("tags") or []):
            continue
        rows.append({"id": pid, "kind": m.get("kind"), "name": m.get("name") or pid,
                     "description": m.get("description") or "", "tags": m.get("tags") or [],
                     "applies_to": m.get("applies_to") or [], "version": m.get("version") or 1,
                     "provenance": m.get("provenance") or {}, "keys": sorted(item["body"].keys()),
                     "used_by": []})
    # 反查哪些任务在用
    for t in task_files():
        td = load_task_file(t) or {}
        for ref in td.get("use") or []:
            for r in rows:
                if r["id"] == ref:
                    r["used_by"].append(((td.get("task") or {}).get("id")) or t.stem)
    return rows


def _dget(obj, dotpath: str):
    cur = obj
    for k in [x for x in dotpath.split(".") if x]:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set(obj: dict, dotpath: str, value):
    parts = [x for x in dotpath.split(".") if x]
    cur = obj
    for k in parts[:-1]:
        cur = cur.setdefault(k, {})
    cur[parts[-1]] = value


def extract_preset(cfg: dict, section: str, preset_id: str, kind: str, name: str,
                   description: str = "", tags: list[str] | None = None, applies_to: list[str] | None = None,
                   provenance: dict | None = None, overwrite: bool = False) -> Path:
    """提炼:把 cfg 的某一段(点路径,如 scope.regions / quality.assertions)存成参数库条目。"""
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS} 之一")
    value = _dget(cfg, section)
    if value is None:
        raise ValueError(f"配置里没有 {section}")
    body: dict = {}
    _set(body, section, copy.deepcopy(value))
    path = LIBRARY_DIR / kind / f"{preset_id.replace('.', '_')}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"参数库条目已存在:{path.name}(overwrite=True 覆盖)")
    prov = dict(provenance or {})
    prov.setdefault("section", section)
    prov.setdefault("extracted_at", datetime.utcnow().strftime("%Y-%m-%d"))
    doc = {"preset": {"id": preset_id, "kind": kind, "name": name, "description": description,
                      "tags": list(tags or []), "applies_to": list(applies_to or []), "version": 1,
                      "provenance": prov},
           "body": body}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ---------------- 任务 ----------------

def task_files() -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(TASKS_DIR / "*.yaml")) if "template" not in Path(p).name)


def load_task_file(path: Path) -> dict | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None


def find_task(task_id: str) -> dict | None:
    direct = TASKS_DIR / f"{task_id}.yaml"
    cands = [direct] + [p for p in task_files() if p != direct]
    for p in cands:
        if not p.exists():
            continue
        d = load_task_file(p)
        if d and ((d.get("task") or {}).get("id") == task_id):
            d["_path"] = str(p)
            return d
    return None


def list_tasks() -> list[dict]:
    out = []
    for p in task_files():
        d = load_task_file(p) or {}
        t = d.get("task") or {}
        out.append({"id": t.get("id") or p.stem, "name": t.get("name") or p.stem, "status": t.get("status") or "draft",
                    "owner": t.get("owner"), "purpose": t.get("purpose"), "schedule": t.get("schedule") or {},
                    "use": list(d.get("use") or []), "file": p.name})
    return out


def compile_task(task: dict, lib: dict | None = None) -> dict:
    """任务 → 画像。返回编译后的画像 dict(顶层带 task 元信息与 compiled_from)。"""
    lib = lib if lib is not None else library()
    meta = dict(task.get("task") or {})
    if not meta.get("id"):
        raise ValueError("task.id 必填")
    cfg: dict = {}
    missing = []
    for ref in task.get("use") or []:
        item = lib.get(str(ref))
        if item is None:
            missing.append(str(ref))
            continue
        cfg = merge(cfg, item["body"])
    if missing:
        raise KeyError(f"参数库里没有:{', '.join(missing)}")
    cfg = merge(cfg, task.get("overrides") or {})
    # 任务元信息 → 画像键
    need = dict(cfg.get("need") or {})
    need["id"] = meta["id"]
    need.setdefault("name", meta.get("name") or meta["id"])
    if meta.get("owner"):
        need.setdefault("owner", meta["owner"])
    if meta.get("priority"):
        need["priority"] = meta["priority"]
    sched = dict(meta.get("schedule") or {})
    if sched.get("cadence"):
        need["timeliness_sla"] = _CADENCE.get(str(sched["cadence"]), str(sched["cadence"]))
    cfg["need"] = need
    if sched.get("time_window_days") is not None:
        cfg.setdefault("scope", {})["time_window_days"] = int(sched["time_window_days"])
    if sched.get("time_filters"):
        cfg.setdefault("keywords", {})["time_filters"] = list(sched["time_filters"])
    budget = dict(meta.get("budget") or {})
    if budget.get("query_budget_per_source_daily"):
        cfg.setdefault("keywords", {})["query_budget_per_source_daily"] = int(budget["query_budget_per_source_daily"])
    if budget.get("max_pages_per_query"):
        cfg.setdefault("keywords", {})["max_pages_per_query"] = int(budget["max_pages_per_query"])
    cfg["task"] = {k: v for k, v in meta.items()}
    cfg["compiled_from"] = {"use": list(task.get("use") or []), "compiled_at": datetime.utcnow().isoformat(timespec="seconds")}
    return cfg


def compile_task_id(task_id: str) -> dict:
    t = find_task(task_id)
    if t is None:
        raise FileNotFoundError(f"没有任务文件:config/tasks/{task_id}.yaml")
    return compile_task(t)


def task_status(cfg: dict | None) -> str:
    """编译后画像里的任务状态;非任务模式的画像视为 active。"""
    return str(((cfg or {}).get("task") or {}).get("status") or "active")


def is_runnable(cfg: dict | None, now: datetime | None = None) -> bool:
    """任务是否该被自动化执行:状态 active 且在有效期内。"""
    t = (cfg or {}).get("task") or {}
    if task_status(cfg) != "active":
        return False
    now = now or datetime.utcnow()
    s = t.get("schedule") or {}
    try:
        if s.get("start") and datetime.fromisoformat(str(s["start"])) > now:
            return False
        if s.get("end") and datetime.fromisoformat(str(s["end"])) < now:
            return False
    except ValueError:
        pass
    return True
