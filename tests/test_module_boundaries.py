"""模块边界守护:分层依赖只能向下,同层不成环,服务层不引用接口层。

分层(见 design/platform/08):
- L0 契约与底座:被所有层依赖,自己不依赖上层;
- L1 能力模块:只依赖 L0 与同层的公开函数,不成环;
- L2 编排:可依赖任何下层。
这里连函数内部的延迟 import 也算依赖——延迟只是绕开导入时序,耦合依然存在。"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "app" / "services"

L0 = {"errors", "diagnostics", "url_tools", "simhash", "need_ctx", "prompts",
      "llm", "fetcher", "archive", "reputation", "settings_service", "actions", "notify"}
L1 = {"adapters", "columns", "keywords", "extraction", "money_guard", "dedup", "events", "followup",
      "leads", "kpi", "digest", "coverage", "discovery", "grading", "query_evolution", "health",
      "wechat", "review", "verify", "relations", "exports"}
L2 = {"pipeline", "prospect", "scheduler", "crawl_runner", "locate", "autopilot", "bootstrap", "daily",
      "profiles", "capabilities"}
LAYER = {**{m: 0 for m in L0}, **{m: 1 for m in L1}, **{m: 2 for m in L2}}


def _deps(path: pathlib.Path) -> tuple[set[str], bool]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps, api = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app.services":
                deps |= {a.name for a in node.names}
            elif node.module.startswith("app.services."):
                deps.add(node.module.split(".")[2])
            elif node.module.startswith("app.api"):
                api = True
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("app.services."):
                    deps.add(a.name.split(".")[2])
                if a.name.startswith("app.api"):
                    api = True
    return deps, api


def _graph():
    g = {}
    for p in ROOT.glob("*.py"):
        if p.stem != "__init__":
            g[p.stem] = _deps(p)
    return g


def test_every_service_module_is_assigned_a_layer():
    mods = {p.stem for p in ROOT.glob("*.py")} - {"__init__"}
    assert mods == set(LAYER), f"未分层的模块:{mods - set(LAYER)};多余:{set(LAYER) - mods}"


def test_no_upward_dependencies():
    bad = []
    for m, (deps, api) in _graph().items():
        for d in deps:
            if d in LAYER and LAYER[d] > LAYER[m]:
                bad.append(f"{m}(L{LAYER[m]}) → {d}(L{LAYER[d]})")
        if api:
            bad.append(f"{m} → app.api(服务层不得引用接口层)")
    assert not bad, "\n".join(bad)


def test_no_cycles_among_capability_modules():
    g = {m: {d for d in deps if d in L1} for m, (deps, _api) in _graph().items() if m in L1}
    seen, stack, cycles = set(), [], []

    def dfs(n):
        if n in stack:
            cycles.append(" → ".join(stack[stack.index(n):] + [n]))
            return
        if n in seen:
            return
        seen.add(n)
        stack.append(n)
        for d in g.get(n, ()):
            dfs(d)
        stack.pop()

    for m in g:
        dfs(m)
    assert not cycles, "\n".join(cycles)


def test_contract_layer_only_depends_on_itself():
    for m in ("need_ctx", "prompts", "url_tools", "notify"):
        deps, api = _deps(ROOT / f"{m}.py")
        assert not api and deps <= L0, f"{m} 依赖了非底座模块:{deps - L0}"
