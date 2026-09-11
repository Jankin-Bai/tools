#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dep_tree.py — 通用构建/脚本依赖树解析工具。

对目标文件（Makefile 等）做依赖展开，输出完整依赖树。
解析器以"注册表 + 插件"方式组织，新增文件类型只需实现 Parser 子类并注册。

用法：
    python dep_tree.py <目标文件路径>              # 文本树
    python dep_tree.py <目标文件路径> --json        # JSON
    python dep_tree.py <目标文件路径> --dot         # Graphviz DOT
    python dep_tree.py <目标文件路径> --list-parsers # 列出已注册的解析器
    python dep_tree.py <目标文件路径> --no-make     # 禁用 make -pn，强制纯 Python 解析

Makefile 解析采用业界标准方案：默认调用 `make -pn` 获取 GNU Make 完整数据库
（变量已展开、include 已合并、pattern rule 已匹配、vpath 已解析），再解析 Files
段构建依赖图。无 make 时回退到纯 Python 解析器（功能有限）。

参考工具：makefile2graph (lindenb), makefile-graph (dnaeon)
"""

from __future__ import annotations

import os
import re
import sys
import json
import ast
import shlex
import logging
import shutil
import subprocess
import tempfile
import argparse
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Type, Set

logger = logging.getLogger("dep_tree")


# ============================================================
# 数据模型
# ============================================================

class NodeType(str, Enum):
    """依赖树节点类型（继承 str 以便 JSON 序列化时为字符串）。"""
    TARGET = "target"
    FILE = "file"
    SCRIPT = "script"
    SUBMAKE = "submake"
    CIRCULAR = "circular"
    PHONY = "phony"
    EXTERNAL = "external"
    REF = "ref"
    # Python 模式新增
    PY_MODULE = "py_module"   # 项目本地 .py 模块
    STDLIB = "stdlib"         # Python 标准库
    PKG = "pkg"               # 第三方已安装包
    C_EXT = "c_ext"           # C 扩展模块
    FUNCTION = "function"     # 函数/方法（调用图模式）

    @property
    def tag(self) -> str:
        """文本渲染时的类型标签，集中定义消除重复映射。"""
        return {
            NodeType.TARGET: "",
            NodeType.FILE: "(file)",
            NodeType.SCRIPT: "(script)",
            NodeType.SUBMAKE: "(submake)",
            NodeType.CIRCULAR: "(循环!)",
            NodeType.PHONY: "(phony)",
            NodeType.EXTERNAL: "(src)",
            NodeType.REF: "(ref)",
            NodeType.PY_MODULE: "(module)",
            NodeType.STDLIB: "(stdlib)",
            NodeType.PKG: "(pkg)",
            NodeType.C_EXT: "(c-ext)",
            NodeType.FUNCTION: "(func)",
        }.get(self, "")

    @property
    def dot_shape(self) -> str:
        if self in (NodeType.FILE, NodeType.SCRIPT, NodeType.PY_MODULE, NodeType.FUNCTION):
            return "box"
        return "ellipse"

    @property
    def dot_color(self) -> str:
        return {
            NodeType.SCRIPT: "#cc6600",
            NodeType.FILE: "#336699",
            NodeType.PHONY: "#993333",
            NodeType.PY_MODULE: "#2ea043",
            NodeType.STDLIB: "#d29922",
            NodeType.PKG: "#8957e5",
            NodeType.C_EXT: "#bf8700",
            NodeType.FUNCTION: "#1f6feb",
        }.get(self, "#333333")

    @property
    def mermaid_shape(self) -> Tuple[str, str]:
        if self == NodeType.PHONY:
            return '("', '")'
        if self == NodeType.SUBMAKE:
            return ("[[", "]]")
        if self == NodeType.CIRCULAR:
            return ("((", "))")
        if self == NodeType.SCRIPT:
            return ("[/", "/]")
        if self == NodeType.PY_MODULE:
            return ("[", "]")
        if self == NodeType.STDLIB:
            return ("([", "])")
        if self == NodeType.PKG:
            return ("[/", "/]")
        if self == NodeType.C_EXT:
            return ("[(", ")]")
        if self == NodeType.FUNCTION:
            return ("[/", "/]")
        return ("[", "]")


@dataclass(slots=True)
class DepNode:
    """依赖树节点（各解析器统一产出的数据结构）。"""
    name: str
    type: NodeType = NodeType.TARGET
    detail: str = ""
    children: List["DepNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type.value,
            "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass(slots=True)
class MakefileDB:
    """GNU Make 解析后的数据库（target → prerequisites，已全部展开）。"""
    rules: Dict[str, List[str]] = field(default_factory=dict)
    phony: Set[str] = field(default_factory=set)
    default_goal: str = ""
    variables: Dict[str, str] = field(default_factory=dict)
    recipes: Dict[str, List[str]] = field(default_factory=dict)


@dataclass(slots=True)
class CallGraphDB:
    """函数调用图（caller → callees，由 pyan3 或 ast 静态分析产出）。"""
    calls: Dict[str, List[str]] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)   # node_id → 显示名
    locations: Dict[str, str] = field(default_factory=dict)  # node_id → file:line
    entry_points: List[str] = field(default_factory=list)   # 顶层函数（非类方法）


# ============================================================
# 解析器注册表（依赖注入，替代全局可变字典）
# ============================================================

class ParserRegistry:
    """持有扩展名 → Parser 类的映射，支持注入和测试。"""

    def __init__(self) -> None:
        self._parsers: Dict[str, Type[Parser]] = {}

    def register(self, cls: Type[Parser]) -> Type[Parser]:
        for key in cls.extensions:
            if key in self._parsers and self._parsers[key] is not cls:
                logger.warning("Extension '%s' already registered by %s, overwriting with %s",
                               key, self._parsers[key].__name__, cls.__name__)
            self._parsers[key] = cls
        return cls

    def find(self, path: str) -> Optional[Type[Parser]]:
        """O(1) 扩展名查找，O(n) 文件名回退遍历。"""
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext in self._parsers:
            return self._parsers[ext]
        name = os.path.basename(path).lower()
        for parser_cls in self._parsers.values():
            if name in parser_cls.extensions:
                return parser_cls
        return None

    def all_parsers(self) -> List[Type[Parser]]:
        """去重后的所有已注册解析器类。"""
        return list({id(p): p for p in self._parsers.values()}.values())


# 默认全局注册表（供装饰器和 CLI 使用）
DEFAULT_REGISTRY = ParserRegistry()


def register_parser(cls: Type[Parser]) -> Type[Parser]:
    """模块级装饰器，注册到 DEFAULT_REGISTRY。"""
    return DEFAULT_REGISTRY.register(cls)


# ============================================================
# 解析器抽象接口
# ============================================================

class Parser(ABC):
    extensions: List[str] = []

    @classmethod
    def matches(cls, path: str) -> bool:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        name = os.path.basename(path).lower()
        return ext in cls.extensions or name in cls.extensions

    @classmethod
    @abstractmethod
    def from_args(cls, args: argparse.Namespace) -> "Parser":
        """从 CLI 参数构造解析器实例（OCP：新增解析器不改 main()）。"""
        ...

    @property
    def mode_label(self) -> str:
        """输出头部的模式标签，子类可覆盖。"""
        return ""

    @abstractmethod
    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        ...


# ============================================================
# Makefile 后端抽象（Strategy 模式）
# ============================================================

class MakefileDBBackend(ABC):
    """产生 MakefileDB 的后端接口（Strategy 模式）。"""

    @abstractmethod
    def parse(self, path: str) -> Optional[MakefileDB]:
        """解析 Makefile，返回数据库；失败返回 None。"""

    def parse_in_dir(self, cwd: str, makefile_args: List[str]) -> Optional[MakefileDB]:
        """在指定目录解析（支持 -f 参数和变量覆盖）。默认实现委托给 parse()。"""
        for arg in makefile_args:
            if arg.endswith(("Makefile", ".mk", ".mak")):
                return self.parse(os.path.join(cwd, arg))
        return self.parse(os.path.join(cwd, "Makefile"))


# ============================================================
# 后端 1: make -pn（业界标准）
# ============================================================

class MakePNBackend(MakefileDBBackend):
    """通过 `make -pn` 获取 GNU Make 完整数据库。"""

    def parse(self, path: str) -> Optional[MakefileDB]:
        base_dir = os.path.dirname(os.path.abspath(path))
        return self._run_make(base_dir, ["-f", os.path.basename(path)])

    def parse_in_dir(self, cwd: str, makefile_args: List[str]) -> Optional[MakefileDB]:
        return self._run_make(cwd, makefile_args)

    def _run_make(self, cwd: str, makefile_args: List[str]) -> Optional[MakefileDB]:
        """Run `make -pn` in cwd with given args, parse output. Returns None on failure."""
        make_exe = shutil.which("make") or shutil.which("gmake")
        if not make_exe:
            logger.debug("make not found, falling back to pure-Python parser")
            return None

        # MAKE=: prevents recursive $(MAKE) execution so sub-Makefile databases
        # don't pollute the parent's output. Submake recursion is handled
        # explicitly by SubmakeResolver.
        cmd = [make_exe, "-pn", "MAKE=:"] + makefile_args
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=30, errors="replace",
            )
        except subprocess.TimeoutExpired:
            logger.warning("make -pn timed out after 30s in %s", cwd)
            return None
        except OSError as e:
            logger.warning("make -pn failed to execute: %s", e)
            return None

        if result.returncode != 0:
            logger.warning("make exited with code %d: %s",
                           result.returncode, result.stderr.strip()[:500])
        if "# Files" not in result.stdout:
            logger.warning("make output has no Files section (cwd=%s)", cwd)
            return None

        # Find the Makefile path for default-goal detection
        makefile_path = os.path.join(cwd, "Makefile")
        for arg in makefile_args:
            if arg.endswith(("Makefile", ".mk", ".mak")):
                makefile_path = os.path.join(cwd, arg)
                break

        return self._parse_db(result.stdout, makefile_path)

    @staticmethod
    def parse_dirty(cwd: str, makefile_args: Optional[List[str]] = None) -> Set[str]:
        """Run `make -nd` and return the set of targets that need rebuilding.

        Parses debug output lines:
          'Must remake target xxx'     -> dirty
          'No need to remake target x' -> clean
        """
        make_exe = shutil.which("make") or shutil.which("gmake")
        if not make_exe:
            return set()

        cmd = [make_exe, "-nd", "MAKE=:"] + (makefile_args or [])
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                timeout=30, errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError):
            return set()

        dirty: Set[str] = set()
        for line in result.stdout.splitlines():
            m = re.match(r"\s*Must remake target '?([^']+)'?\.?", line)
            if m:
                dirty.add(m.group(1))
        return dirty

    def _parse_db(self, output: str, makefile_path: str) -> MakefileDB:
        db = MakefileDB()
        db.default_goal = find_default_goal(makefile_path)
        lines = output.splitlines()

        self._parse_variables(lines, db)

        # Find Files section
        in_files = False
        i = 0
        while i < len(lines):
            line = lines[i]

            if line.startswith("# Files"):
                in_files = True
                i += 1
                continue
            if in_files and line.startswith("# files hash-table stats"):
                break
            if not in_files:
                i += 1
                continue

            # Skip "Not a target" entries (suffixes like .cpp:)
            if line.startswith("# Not a target:"):
                i += 1
                while i < len(lines) and (lines[i].startswith("#") or lines[i].startswith("\t")):
                    i += 1
                continue

            # Target line: "target1 target2: prereq1 prereq2" (supports multi-target)
            m = re.match(r'^(\S[^:]*):\s*(.*)$', line)
            if m and not line.startswith("#") and not line.startswith("\t"):
                targets = m.group(1).split()
                prereqs = m.group(2).split()
                is_phony = False
                recipe_lines: List[str] = []

                # Scan associated comments and recipe lines
                j = i + 1
                while j < len(lines):
                    if lines[j].startswith("#"):
                        if "Phony target" in lines[j]:
                            is_phony = True
                        j += 1
                    elif lines[j].startswith("\t"):
                        recipe_lines.append(lines[j].lstrip("\t"))
                        j += 1
                    else:
                        break

                for target in targets:
                    if target and not target.startswith("."):
                        db.rules[target] = prereqs
                        if is_phony:
                            db.phony.add(target)
                        if recipe_lines:
                            db.recipes[target] = recipe_lines
                i = j
                continue

            i += 1

        return db

    @staticmethod
    def _parse_variables(lines: List[str], db: MakefileDB) -> None:
        """Parse the Variables section of `make -pn` output."""
        in_vars = False
        for line in lines:
            if line.startswith("# Variables"):
                in_vars = True
                continue
            if in_vars and line.startswith("# Files"):
                break
            if not in_vars or line.startswith("#") or not line.strip():
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*(:=|\?=|\+=|=)\s*(.*)$', line)
            if m:
                name, op, value = m.group(1), m.group(2), m.group(3)
                if op == "?=":
                    if name not in db.variables:
                        db.variables[name] = value
                elif op == "+=":
                    old = db.variables.get(name, "")
                    db.variables[name] = (old + " " + value).strip() if old else value
                else:
                    db.variables[name] = value


# ============================================================
# 后端 2: 纯 Python 解析（fallback，功能有限）
# ============================================================

class PurePythonBackend(MakefileDBBackend):
    """手写 Makefile 解析器，仅在无 make 时使用。"""

    def parse(self, path: str) -> Optional[MakefileDB]:
        if not os.path.exists(path):
            return None
        db = MakefileDB()
        variables, raw_rules, phony = self._parse_file(path)
        env = dict(variables)

        for rule in raw_rules:
            for target in rule["targets"]:
                t_exp = self._expand(target, env).strip()
                if "%" in t_exp or not t_exp:
                    continue
                prereqs_exp: List[str] = []
                for p in rule["prereqs"]:
                    p_exp = self._expand(p, env).strip()
                    prereqs_exp.extend(p_exp.split())
                db.rules[t_exp] = prereqs_exp

        db.phony = {self._expand(p, env) for p in phony}

        # Default goal = first non-special target
        for rule in raw_rules:
            for t in rule["targets"]:
                t_exp = self._expand(t, env).strip()
                if t_exp and not t_exp.startswith(".") and "%" not in t_exp:
                    db.default_goal = t_exp
                    break
            if db.default_goal:
                break
        return db

    def _parse_file(self, path: str, _seen: Optional[set] = None
                    ) -> Tuple[Dict[str, str], List[Dict], set]:
        if _seen is None:
            _seen = set()
        abs_path = os.path.abspath(path)
        if abs_path in _seen or not os.path.exists(abs_path):
            return {}, [], set()
        _seen.add(abs_path)

        variables: Dict[str, str] = {}
        rules: List[Dict] = []
        phony: set = set()
        base_dir = os.path.dirname(abs_path)

        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        content = re.sub(r'\\\n\s*', ' ', content)

        for raw_line in content.splitlines():
            code = raw_line.split("#", 1)[0].rstrip() if not raw_line.lstrip().startswith("#") else ""
            if not code or raw_line.startswith("\t"):
                continue

            # include / -include / sinclude
            m_inc = re.match(r'^\s*(-?include|sinclude)\s+(.+)$', code)
            if m_inc:
                for inc in m_inc.group(2).split():
                    inc_path = inc if os.path.isabs(inc) else os.path.join(base_dir, inc)
                    iv, ir, ip = self._parse_file(inc_path, _seen)
                    variables.update(iv)
                    rules.extend(ir)
                    phony.update(ip)
                continue

            # .PHONY
            m_ph = re.match(r'^\.PHONY\s*:\s*(.+)$', code)
            if m_ph:
                phony.update(m_ph.group(1).split())
                continue

            # Variable assignment
            m_var = self._match_assignment(code)
            if m_var:
                name, op, value = m_var
                if op == "?=":
                    if name not in variables:
                        variables[name] = value
                elif op == "+=":
                    old = variables.get(name, "")
                    variables[name] = (old + " " + value).strip() if old else value
                else:
                    variables[name] = value
                continue

            # Rule
            m_rule = self._match_rule(code)
            if m_rule:
                targets, prereqs = m_rule
                rules.append({"targets": targets.split(), "prereqs": prereqs.split()})

        return variables, rules, phony

    @staticmethod
    def _match_assignment(code: str) -> Optional[Tuple[str, str, str]]:
        for op in (":=", "?=", "+="):
            idx = code.find(op)
            if idx > 0:
                name = code[:idx].strip()
                if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
                    return (name, op, code[idx + 2:].strip())
        idx = code.find("=")
        if idx > 0 and code[idx - 1] not in ":?+":
            name = code[:idx].strip()
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
                return (name, "=", code[idx + 1:].strip())
        return None

    @staticmethod
    def _match_rule(code: str) -> Optional[Tuple[str, str]]:
        idx = code.find(":")
        if idx <= 0:
            return None
        if idx + 1 < len(code) and code[idx + 1] == "=":
            return None
        # Windows drive letter C:\ — skip first colon, find next
        if idx == 1 and code[0].isalpha() and idx + 1 < len(code) and code[idx + 1] == "\\":
            idx2 = code.find(":", idx + 1)
            if idx2 <= 0:
                return None
            idx = idx2
        name_part = code[:idx].strip()
        if not name_part or any(c in name_part for c in "=+?"):
            return None
        return (name_part, code[idx + 1:].strip())

    def _expand(self, value: str, env: Dict[str, str], _depth: int = 0) -> str:
        if _depth > 15 or not value or "$" not in value:
            return value

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name in env:
                return self._expand(env[name], env, _depth + 1)
            return m.group(0)

        return re.sub(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)', repl, value)


# ============================================================
# 共享工具：扫描 Makefile 找默认目标（复用 PurePythonBackend）
# ============================================================

def find_default_goal(path: str) -> str:
    """扫描 Makefile 第一个显式目标（GNU Make 的 default goal）。

    复用 PurePythonBackend 的解析逻辑，避免重复实现指令/规则匹配。
    """
    db = PurePythonBackend().parse(path)
    if db and db.default_goal:
        return db.default_goal
    return "all"


# ============================================================
# Recipe 分析常量与变量展开
# ============================================================

# $(MAKE) -C dir  或  make -C dir  （含后续变量传递）
_SUBMAKE_RE = re.compile(r'\$\(MAKE\)\s+-C\s+(\S+)(.*)|(?:^|\s)make\s+-C\s+(\S+)(.*)')
# 脚本文件引用：*.py / *.sh / *.bat / *.cmd（排除 -c "..." 内联代码）
_SCRIPT_EXT_RE = re.compile(r'(\S+\.(?:py|sh|bat|cmd|ps1))\b')
# 内联 python -c "..." 标记（跳过）
_INLINE_CODE_RE = re.compile(r'\s-c\s')


def _expand_vars(text: str, variables: Dict[str, str], _depth: int = 0) -> str:
    """在 recipe 行中展开 $(VAR) / ${VAR}（简单展开，不处理函数）。"""
    if _depth > 10 or not text or "$" not in text:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name in variables:
            return _expand_vars(variables[name], variables, _depth + 1)
        return m.group(0)

    return re.sub(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}', repl, text)


# ============================================================
# Recipe 扫描结果（值对象）
# ============================================================

@dataclass(slots=True)
class SubmakeCall:
    """recipe 中检测到的 $(MAKE) -C 调用。"""
    dir: str
    extra_args: str


@dataclass(slots=True)
class ScriptRef:
    """recipe 中检测到的脚本文件引用。"""
    path: str


# ============================================================
# RecipeScanner — 从 recipe 行提取 submake 调用和脚本依赖
# ============================================================

class RecipeScanner:
    """无状态扫描器：从 recipe 行中提取 submake 调用和脚本引用。

    原属于 GraphBuilder 的职责，拆出后 GraphBuilder 只负责图构建。
    """

    def scan(self, recipe_lines: List[str], variables: Dict[str, str]
             ) -> Tuple[List[SubmakeCall], List[ScriptRef]]:
        submakes: List[SubmakeCall] = []
        scripts: List[ScriptRef] = []

        for raw_line in recipe_lines:
            line = raw_line.lstrip("@-").strip()
            if not line:
                continue

            # --- Submake detection ---
            # Check for $(MAKE) in raw line first (MAKE=: override would
            # expand it to ':' and break the regex). Then expand for dir/args.
            has_make_var = '$(MAKE)' in line or '${MAKE}' in line
            expanded = _expand_vars(line, variables)
            if has_make_var:
                # Restore 'make' keyword for regex matching
                expanded = re.sub(r'^:\s+', 'make ', expanded)
                expanded = re.sub(r'(?<=\s):\s+', 'make ', expanded)

            found_submake = False
            for sm in _SUBMAKE_RE.finditer(expanded):
                sub_dir = sm.group(1) or sm.group(3)
                extra_args = (sm.group(2) or sm.group(4) or "").strip()
                submakes.append(SubmakeCall(dir=sub_dir, extra_args=extra_args))
                found_submake = True

            if found_submake:
                continue  # submake line rarely also calls scripts

            # --- Script detection (skip inline -c code) ---
            if _INLINE_CODE_RE.search(expanded):
                continue
            for script_match in _SCRIPT_EXT_RE.finditer(expanded):
                script_path = script_match.group(1).strip('"\'')
                scripts.append(ScriptRef(path=script_path))

        return submakes, scripts


# ============================================================
# SubmakeResolver — 递归解析子 Makefile
# ============================================================

class SubmakeResolver:
    """递归解析子工程 Makefile，带缓存防重复。

    原属于 GraphBuilder._add_submake()，拆出后：
    - 缓存通过实例属性管理，不再穿透 GraphBuilder 私有成员
    - 后端通过构造函数注入，不直接实例化 MakePNBackend
    """

    def __init__(self, backends: List[MakefileDBBackend], dirty: bool = False) -> None:
        self.backends = backends
        self.dirty = dirty
        self._cache: Dict[str, DepNode] = {}

    def resolve(self, parent_base_dir: str, sub_dir: str, extra_args: str,
                seen: set) -> DepNode:
        sub_abs = sub_dir if os.path.isabs(sub_dir) else os.path.join(parent_base_dir, sub_dir)
        sub_abs = os.path.normpath(sub_abs)
        sub_makefile = os.path.join(sub_abs, "Makefile")

        if not os.path.exists(sub_makefile):
            return DepNode(
                name=sub_dir.replace(os.sep, "/"), type=NodeType.SUBMAKE,
                detail="(子工程 Makefile 不存在)")

        cache_key = sub_abs
        if cache_key in self._cache:
            return DepNode(
                name=sub_dir.replace(os.sep, "/"), type=NodeType.REF,
                detail="(子工程见上方)")
        if cache_key in seen:
            return DepNode(
                name=sub_dir.replace(os.sep, "/"), type=NodeType.CIRCULAR,
                detail="(循环子工程)")

        # Parse extra_args: separate targets, variable assignments, flags
        try:
            tokens = shlex.split(extra_args)
        except ValueError:
            tokens = extra_args.split()

        make_args: List[str] = []
        sub_goal: Optional[str] = None
        for tok in tokens:
            if tok.startswith("-"):
                continue  # skip flags (-j, -k, etc.)
            if "=" in tok:
                make_args.append(tok)  # variable override
            elif sub_goal is None:
                sub_goal = tok  # first non-flag, non-assignment token = target

        # Run backends in subdirectory with inherited variables (策略链)
        sub_db: Optional[MakefileDB] = None
        for backend in self.backends:
            sub_db = backend.parse_in_dir(sub_abs, make_args)
            if sub_db is not None:
                break

        if sub_db is None:
            return DepNode(
                name=sub_dir.replace(os.sep, "/"), type=NodeType.SUBMAKE,
                detail="(子工程解析失败: %s)" % sub_dir)

        # Dirty detection (only if enabled and a MakePNBackend exists)
        sub_dirty: Set[str] = set()
        if self.dirty:
            for backend in self.backends:
                if isinstance(backend, MakePNBackend):
                    sub_dirty = MakePNBackend.parse_dirty(sub_abs, make_args)
                    break

        # Placeholder to prevent infinite recursion during sub-build
        self._cache[cache_key] = DepNode(name="placeholder")

        # Build sub-tree (fresh seen set — target names are per-Makefile)
        sub_builder = GraphBuilder(
            sub_abs, dirty=sub_dirty,
            scanner=RecipeScanner(), submake_resolver=self)
        goal = sub_goal or sub_db.default_goal or "all"
        sub_tree = sub_builder.build(goal, sub_db, seen={cache_key})

        # Detail: show the submake command (goal + variable overrides)
        detail_parts = [
            "子工程 Makefile: %s" % os.path.relpath(sub_makefile, parent_base_dir).replace(os.sep, "/")
        ]
        if sub_goal:
            detail_parts.append("目标: %s" % sub_goal)
        if make_args:
            var_names = []
            for arg in make_args:
                name = arg.split("=", 1)[0]
                value = arg.split("=", 1)[1] if "=" in arg else ""
                if len(value) > 40:
                    value = value[:37] + "..."
                var_names.append("%s=%s" % (name, value))
            detail_parts.append("变量: %s" % " ".join(var_names))

        sub_node = DepNode(
            name=sub_dir.replace(os.sep, "/"),
            type=NodeType.SUBMAKE,
            detail=" | ".join(detail_parts))
        sub_node.children = [sub_tree]
        self._cache[cache_key] = sub_node
        return sub_node


# ============================================================
# GraphBuilder — 从 MakefileDB 构建 DepNode 树
# ============================================================

class GraphBuilder:
    """从 MakefileDB 递归构建 DepNode 树。

    recipe 扫描委托给 RecipeScanner，submake 递归委托给 SubmakeResolver。
    """

    def __init__(self, base_dir: str, dirty: Optional[Set[str]] = None,
                 scanner: Optional[RecipeScanner] = None,
                 submake_resolver: Optional[SubmakeResolver] = None) -> None:
        self.base_dir = base_dir
        self.dirty = dirty or set()
        self.scanner = scanner or RecipeScanner()
        self.submake_resolver = submake_resolver

    def build(self, target: str, db: MakefileDB,
              seen: Optional[set] = None,
              expanded: Optional[set] = None) -> DepNode:
        if seen is None:
            seen = set()
        if expanded is None:
            expanded = set()

        if target in seen:
            return DepNode(name=target, type=NodeType.CIRCULAR, detail="(循环依赖)")
        if target in expanded:
            return DepNode(name=target, type=NodeType.REF, detail="(见上方)")

        is_phony = target in db.phony
        node = DepNode(name=target, type=NodeType.PHONY if is_phony else NodeType.TARGET)
        if target in self.dirty:
            node.detail = "(需重建)"
        seen = seen | {target}

        prereqs = db.rules.get(target)
        if prereqs is None:
            self._mark_file_or_external(node, target)
            return node

        expanded.add(target)
        for p in prereqs:
            node.children.append(self.build(p, db, seen, expanded))

        # Scan recipe lines for submake calls and script dependencies (委托)
        recipe_lines = db.recipes.get(target)
        if recipe_lines:
            submakes, scripts = self.scanner.scan(recipe_lines, db.variables)
            for sm in submakes:
                if self.submake_resolver:
                    node.children.append(
                        self.submake_resolver.resolve(self.base_dir, sm.dir, sm.extra_args, seen))
                else:
                    node.children.append(DepNode(
                        name=sm.dir, type=NodeType.SUBMAKE, detail="(no resolver)"))
            for s in scripts:
                node.children.append(self._make_script_node(s.path))

        # Build artifact that exists on disk
        if node.type == NodeType.TARGET and not is_phony:
            full = os.path.join(self.base_dir, target)
            if os.path.exists(full):
                node.type = NodeType.FILE
                node.detail = "(构建产物)"

        return node

    def _make_script_node(self, script_path: str) -> DepNode:
        """添加脚本依赖节点。"""
        abs_script = script_path if os.path.isabs(script_path) else os.path.join(self.base_dir, script_path)
        abs_script = os.path.normpath(abs_script)
        if os.path.exists(abs_script):
            rel = os.path.relpath(abs_script, self.base_dir).replace(os.sep, "/")
            return DepNode(
                name=os.path.basename(script_path),
                type=NodeType.SCRIPT,
                detail=rel)
        return DepNode(
            name=os.path.basename(script_path),
            type=NodeType.EXTERNAL,
            detail="(脚本未找到: %s)" % script_path)

    def _mark_file_or_external(self, node: DepNode, target: str) -> None:
        full = target if os.path.isabs(target) else os.path.join(self.base_dir, target)
        if os.path.exists(full):
            node.type = NodeType.FILE
            node.detail = os.path.relpath(full, self.base_dir).replace(os.sep, "/")
        else:
            node.type = NodeType.EXTERNAL
            node.detail = "(源文件)"


# ============================================================
# Makefile 解析器（Facade：后端策略链 + 构建图）
# ============================================================

@register_parser
class MakefileParser(Parser):
    """Makefile 依赖解析器。

    Facade：按顺序尝试 backends 列表，第一个成功的使用。
    """

    extensions = ["makefile", "mk", "mak", "makfile"]

    def __init__(self, backends: List[MakefileDBBackend], check_dirty: bool = False) -> None:
        self.backends = backends
        self.check_dirty = check_dirty

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "MakefileParser":
        backends: List[MakefileDBBackend] = []
        if not args.no_make:
            backends.append(MakePNBackend())
        backends.append(PurePythonBackend())
        return cls(backends=backends, check_dirty=args.dirty)

    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        abs_path = os.path.abspath(path)
        base_dir = os.path.dirname(abs_path)

        # 策略链：按顺序尝试后端，第一个返回非 None 的使用
        db: Optional[MakefileDB] = None
        for backend in self.backends:
            db = backend.parse(abs_path)
            if db is not None:
                logger.debug("Using backend: %s", type(backend).__name__)
                break

        if db is None:
            logger.warning("All backends failed, using empty database")
            db = MakefileDB()

        dirty: Set[str] = set()
        if self.check_dirty:
            for backend in self.backends:
                if isinstance(backend, MakePNBackend):
                    dirty = MakePNBackend.parse_dirty(base_dir)
                    break

        resolver = SubmakeResolver(self.backends, dirty=self.check_dirty)
        goal = root or db.default_goal or "all"
        tree = GraphBuilder(base_dir, dirty=dirty, submake_resolver=resolver).build(goal, db)

        if not root:
            agg = DepNode(name=os.path.basename(path), type=NodeType.FILE, detail=abs_path)
            agg.children = [tree]
            return agg
        return tree


# ============================================================
# 共享工具：项目虚拟环境检测
# ============================================================

_VENV_DIRS = (".venv", "venv", "env")


def find_project_venv(start_path: str, max_up: int = 8) -> Optional[str]:
    """从 start_path 向上查找项目虚拟环境，返回其 python.exe 路径。

    查找标记: .venv / venv / env 目录下的 Scripts/python.exe (Windows)
    或 bin/python (Unix)。最多向上遍历 max_up 级目录。
    """
    current = os.path.dirname(os.path.abspath(start_path))
    for _ in range(max_up):
        for vdir in _VENV_DIRS:
            candidate = os.path.join(current, vdir)
            if os.path.isdir(candidate):
                for py_rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
                    py_path = os.path.join(candidate, py_rel)
                    if os.path.isfile(py_path):
                        return py_path
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def resolve_python_exe(explicit: Optional[str], start_path: str) -> str:
    """解析要使用的 Python 可执行文件路径。

    优先级: explicit > 项目 venv > sys.executable
    """
    if explicit and os.path.isfile(explicit):
        return explicit
    venv_py = find_project_venv(start_path)
    if venv_py:
        return venv_py
    return sys.executable


# ============================================================
# 共享工具：Python 模块分类
# ============================================================

def _get_stdlib_names() -> Set[str]:
    """获取标准库模块名集合。优先 sys.stdlib_module_names (3.10+)，
    回退 stdlib_list 包，最后回退硬编码常见列表。
    """
    if hasattr(sys, "stdlib_module_names"):
        return set(sys.stdlib_module_names)
    try:
        from stdlib_list import stdlib_list
        return set(stdlib_list())
    except ImportError:
        pass
    # 最小回退集（仅用于分类判断，不追求完整）
    return {
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "concurrent",
        "configparser", "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime",
        "decimal", "difflib", "email", "enum", "errno", "fnmatch", "functools", "gc",
        "getopt", "getpass", "glob", "gzip", "hashlib", "heapq", "hmac", "html", "http",
        "importlib", "inspect", "io", "ipaddress", "itertools", "json", "keyword",
        "locale", "logging", "math", "mimetypes", "multiprocessing", "numbers", "operator",
        "os", "pathlib", "pickle", "platform", "plistlib", "pprint", "queue", "random",
        "re", "selectors", "shlex", "shutil", "signal", "site", "smtplib", "socket",
        "socketserver", "sqlite3", "ssl", "stat", "string", "struct", "subprocess",
        "sys", "tarfile", "tempfile", "textwrap", "threading", "time", "timeit",
        "trace", "traceback", "types", "typing", "unicodedata", "unittest", "urllib",
        "uuid", "warnings", "weakref", "webbrowser", "xml", "zipfile", "zlib",
    }


_STDLIB_NAMES = _get_stdlib_names()


def _classify_module(name: str, local_dirs: Set[str]) -> NodeType:
    """根据模块名和项目本地目录判断模块类型。"""
    top = name.split(".")[0]
    if top in _STDLIB_NAMES:
        return NodeType.STDLIB
    # 检查是否为项目本地模块
    for d in local_dirs:
        candidate = os.path.join(d, top.replace(".", os.sep) + ".py")
        candidate_pkg = os.path.join(d, top.replace(".", os.sep), "__init__.py")
        if os.path.isfile(candidate) or os.path.isfile(candidate_pkg):
            return NodeType.PY_MODULE
    return NodeType.PKG


# ============================================================
# Python 源码解析器（pydeps 主后端 + ast fallback）
# ============================================================

@register_parser
class PythonSourceParser(Parser):
    """Python 源码依赖解析器。

    主后端: pydeps --show-deps（成熟工具，准确处理动态导入）
    Fallback: 纯 ast 静态解析（零依赖，功能有限）
    """

    extensions = ["py"]

    def __init__(self, python_exe: Optional[str] = None, no_stdlib: bool = False,
                 max_depth: int = 10, use_pydeps: bool = True) -> None:
        self.python_exe = python_exe
        self.no_stdlib = no_stdlib
        self.max_depth = max_depth
        self.use_pydeps = use_pydeps

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "PythonSourceParser":
        return cls(
            python_exe=args.python,
            no_stdlib=args.no_stdlib,
            max_depth=args.max_depth,
            use_pydeps=not args.no_pydeps,
        )

    @property
    def mode_label(self) -> str:
        return "Source Mode"

    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        abs_path = os.path.abspath(path)
        py = resolve_python_exe(self.python_exe, abs_path)
        base_dir = os.path.dirname(abs_path)

        if self.use_pydeps:
            tree = self._parse_with_pydeps(abs_path, py, base_dir)
            if tree is not None:
                return tree
            logger.info("pydeps failed or unavailable, falling back to ast parser")

        return self._parse_with_ast(abs_path, base_dir)

    # --- pydeps 主后端 ---

    def _parse_with_pydeps(self, path: str, python_exe: str,
                            base_dir: str) -> Optional[DepNode]:
        """调用 pydeps --show-deps 获取 JSON 模块图，构建 DepNode 树。"""
        try:
            result = subprocess.run(
                [python_exe, "-m", "pydeps", "--show-deps", "--no-output", path],
                capture_output=True, text=True, timeout=60,
                errors="replace", cwd=base_dir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("pydeps execution failed: %s", e)
            return None

        if result.returncode != 0:
            logger.warning("pydeps exited with code %d: %s",
                           result.returncode, result.stderr.strip()[:300])
            return None

        try:
            dep_graph = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("pydeps JSON parse failed: %s", e)
            return None

        return self._build_tree_from_pydeps(dep_graph, path, base_dir)

    def _build_tree_from_pydeps(self, dep_graph: dict, path: str,
                                 base_dir: str) -> DepNode:
        """从 pydeps JSON 构建 DepNode 树。

        pydeps 输出格式: {module_name: {name, path, imports, imported_by, kind, bacon}}
        """
        target_name = os.path.basename(path)
        local_dirs = {base_dir}

        # 找到目标模块节点（pydeps 可能用文件名或模块名作为 key）
        target_key = None
        for key in dep_graph:
            if key == target_name or key.endswith(target_name):
                target_key = key
                break
        if target_key is None and dep_graph:
            # 取 bacon=0 的节点作为目标
            for key, info in dep_graph.items():
                if info.get("bacon", 1) == 0:
                    target_key = key
                    break
        if target_key is None:
            if not dep_graph:
                return DepNode(name=os.path.basename(path), type=NodeType.PY_MODULE,
                               detail=path,
                               children=[DepNode(name="(no imports found)",
                                                 type=NodeType.EXTERNAL)])
            target_key = next(iter(dep_graph))

        visited: Set[str] = set()

        def build_node(name: str, depth: int = 0) -> DepNode:
            info = dep_graph.get(name, {})
            mod_path = info.get("path", "")
            kind = info.get("kind", "")

            # 分类
            if kind == "imp.C_EXTENSION":
                ntype = NodeType.C_EXT
            else:
                ntype = _classify_module(name, local_dirs)

            detail = ""
            if mod_path and ntype == NodeType.PY_MODULE:
                try:
                    detail = os.path.relpath(mod_path, base_dir).replace(os.sep, "/")
                except ValueError:
                    detail = mod_path
            elif ntype == NodeType.PKG:
                ver = self._get_package_version(name)
                if ver:
                    detail = "v%s" % ver

            node = DepNode(name=name, type=ntype, detail=detail)

            if self.no_stdlib and ntype == NodeType.STDLIB:
                return node

            if depth >= self.max_depth or name in visited:
                if name in visited:
                    node.type = NodeType.CIRCULAR
                    node.detail = "(循环依赖)"
                return node

            visited.add(name)
            imports = info.get("imports", [])
            for imp in imports:
                child = build_node(imp, depth + 1)
                if not (self.no_stdlib and child.type == NodeType.STDLIB):
                    node.children.append(child)
            visited.discard(name)
            return node

        tree = build_node(target_key)
        agg = DepNode(name=os.path.basename(path), type=NodeType.PY_MODULE,
                       detail=os.path.abspath(path))
        agg.children = [tree]
        return agg

    @staticmethod
    def _get_package_version(name: str) -> Optional[str]:
        """尝试获取已安装包的版本号（使用当前解释器的 importlib.metadata）。"""
        try:
            from importlib.metadata import version
            return version(name)
        except Exception:
            return None

    # --- ast fallback ---

    def _parse_with_ast(self, path: str, base_dir: str) -> DepNode:
        """纯 ast 静态解析 fallback。"""
        local_dirs = {base_dir}
        visited: Set[str] = set()
        file_count = [0]
        stdlib_count = [0]
        pkg_count = [0]

        def parse_file(filepath: str, depth: int = 0) -> DepNode:
            abs_fp = os.path.abspath(filepath)
            mod_name = os.path.splitext(os.path.basename(filepath))[0]

            if depth >= self.max_depth or abs_fp in visited:
                if abs_fp in visited:
                    return DepNode(name=mod_name, type=NodeType.CIRCULAR,
                                   detail="(循环依赖)")
                return DepNode(name=mod_name, type=NodeType.REF, detail="(见上方)")

            visited.add(abs_fp)
            file_count[0] += 1

            try:
                with open(abs_fp, "r", encoding="utf-8-sig", errors="replace") as f:
                    tree = ast.parse(f.read(), filename=abs_fp)
            except (SyntaxError, IOError, OSError) as e:
                visited.discard(abs_fp)
                return DepNode(name=mod_name, type=NodeType.EXTERNAL,
                               detail="(解析失败: %s)" % e)

            try:
                rel = os.path.relpath(abs_fp, base_dir).replace(os.sep, "/")
            except ValueError:
                rel = abs_fp
            node = DepNode(name=mod_name, type=NodeType.PY_MODULE, detail=rel)

            for node_ast in ast.walk(tree):
                if isinstance(node_ast, ast.Import):
                    for alias in node_ast.names:
                        child = self._resolve_and_parse(
                            alias.name, base_dir, local_dirs, visited,
                            depth + 1, parse_file, stdlib_count, pkg_count)
                        if child:
                            node.children.append(child)
                elif isinstance(node_ast, ast.ImportFrom):
                    if node_ast.module and node_ast.level == 0:
                        child = self._resolve_and_parse(
                            node_ast.module, base_dir, local_dirs, visited,
                            depth + 1, parse_file, stdlib_count, pkg_count)
                        if child:
                            node.children.append(child)

            visited.discard(abs_fp)
            return node

        tree = parse_file(path)
        agg = DepNode(name=os.path.basename(path), type=NodeType.PY_MODULE,
                       detail=os.path.abspath(path))
        agg.children = [tree]
        return agg

    def _resolve_and_parse(self, mod_name: str, base_dir: str,
                            local_dirs: Set[str], visited: Set[str],
                            depth: int, parse_fn, stdlib_count, pkg_count):
        """解析模块名，分类，必要时递归解析本地模块。"""
        ntype = _classify_module(mod_name, local_dirs)

        if ntype == NodeType.STDLIB:
            if self.no_stdlib:
                return None
            stdlib_count[0] += 1
            return DepNode(name=mod_name, type=NodeType.STDLIB, detail="(stdlib)")

        if ntype == NodeType.PKG:
            pkg_count[0] += 1
            ver = self._get_package_version(mod_name)
            detail = "v%s" % ver if ver else ""
            return DepNode(name=mod_name, type=NodeType.PKG, detail=detail)

        # 本地模块: 尝试找到文件并递归解析
        top = mod_name.split(".")[0]
        candidate = os.path.join(base_dir, top.replace(".", os.sep) + ".py")
        candidate_pkg = os.path.join(base_dir, top.replace(".", os.sep), "__init__.py")
        target_file = candidate if os.path.isfile(candidate) else candidate_pkg

        if os.path.isfile(target_file):
            return parse_fn(target_file, depth)
        return DepNode(name=mod_name, type=NodeType.PY_MODULE, detail="(未找到源文件)")


# ============================================================
# Python 包依赖解析器（pipdeptree 主后端 + importlib.metadata fallback）
# ============================================================

@register_parser
class PythonPackageParser(Parser):
    """Python 已安装包依赖解析器。

    主后端: pipdeptree -o json（成熟工具，含版本冲突检测）
    Fallback: importlib.metadata（stdlib，零依赖）

    触发文件: requirements.txt, pyproject.toml, setup.cfg
    （文件仅用于 venv 检测，实际分析对象是已安装包）
    """

    extensions = ["requirements.txt", "pyproject.toml", "setup.cfg", "Pipfile"]

    def __init__(self, python_exe: Optional[str] = None, package_name: Optional[str] = None,
                 reverse: bool = False, use_pipdeptree: bool = True) -> None:
        self.python_exe = python_exe
        self.package_name = package_name
        self.reverse = reverse
        self.use_pipdeptree = use_pipdeptree

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "PythonPackageParser":
        return cls(
            python_exe=args.python,
            package_name=args.name,
            reverse=args.reverse,
            use_pipdeptree=not args.no_pipdeptree,
        )

    @property
    def mode_label(self) -> str:
        return "Package Mode (%s)" % ("reverse" if self.reverse else "forward")

    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        abs_path = os.path.abspath(path) if path and os.path.exists(path) else os.getcwd()
        py = resolve_python_exe(self.python_exe, abs_path)

        if self.use_pipdeptree:
            tree = self._parse_with_pipdeptree(py)
            if tree is not None:
                return tree
            logger.info("pipdeptree failed or unavailable, falling back to importlib.metadata")

        return self._parse_with_importlib(py)

    # --- pipdeptree 主后端 ---

    def _parse_with_pipdeptree(self, python_exe: str) -> Optional[DepNode]:
        """调用 pipdeptree -o json 获取包依赖图。"""
        cmd = [python_exe, "-m", "pipdeptree", "-o", "json"]
        if self.package_name:
            cmd.extend(["--packages", self.package_name])
        if self.reverse:
            cmd.append("--reverse")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("pipdeptree execution failed: %s", e)
            return None

        if result.returncode != 0:
            logger.warning("pipdeptree exited with code %d: %s",
                           result.returncode, result.stderr.strip()[:300])
            return None

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("pipdeptree JSON parse failed: %s", e)
            return None

        return self._build_tree_from_pipdeptree(data)

    def _build_tree_from_pipdeptree(self, data: list) -> DepNode:
        """从 pipdeptree JSON 构建 DepNode 树。

        格式: [{package: {key, package_name, installed_version},
                 dependencies: [{key, package_name, installed_version, required_version}]}]
        """
        # 建立包名 -> 依赖列表的映射
        pkg_deps: Dict[str, List[dict]] = {}
        pkg_versions: Dict[str, str] = {}
        for entry in data:
            pkg = entry.get("package", {})
            name = pkg.get("package_name", pkg.get("key", ""))
            ver = pkg.get("installed_version", "")
            pkg_versions[name] = ver
            pkg_deps[name] = entry.get("dependencies", [])

        visited: Set[str] = set()

        def build_node(name: str, depth: int = 0) -> DepNode:
            ver = pkg_versions.get(name, "")
            detail = "v%s" % ver if ver else ""

            if name in visited:
                return DepNode(name=name, type=NodeType.CIRCULAR,
                               detail="%s (循环)" % detail)

            deps = pkg_deps.get(name, [])
            if not deps or depth >= 15:
                return DepNode(name=name, type=NodeType.PKG, detail=detail)

            visited.add(name)
            node = DepNode(name=name, type=NodeType.PKG, detail=detail)
            for dep in deps:
                dep_name = dep.get("package_name", dep.get("key", ""))
                req_ver = dep.get("required_version", "")
                inst_ver = dep.get("installed_version", "")
                child_detail = ""
                if req_ver:
                    child_detail = "requires: %s" % req_ver
                    if inst_ver:
                        child_detail += ", installed: %s" % inst_ver
                elif inst_ver:
                    child_detail = "v%s" % inst_ver
                child = build_node(dep_name, depth + 1)
                if child_detail and not child.detail:
                    child.detail = child_detail
                node.children.append(child)
            visited.discard(name)
            return node

        # 确定根节点
        if self.package_name:
            root_name = self.package_name
        elif data:
            root_name = data[0].get("package", {}).get("package_name", "packages")
        else:
            root_name = "packages"

        if self.package_name and self.package_name in pkg_deps:
            tree = build_node(self.package_name)
        elif len(data) == 1:
            tree = build_node(root_name)
        else:
            # 多个顶层包: 聚合为虚拟根
            tree = DepNode(name="installed-packages", type=NodeType.PKG,
                            detail="%d packages" % len(data))
            for entry in data:
                pkg = entry.get("package", {})
                name = pkg.get("package_name", "")
                if name:
                    tree.children.append(build_node(name))

        agg = DepNode(name="Package Dependencies (%s)" % self.mode_label,
                      type=NodeType.PKG, detail="")
        agg.children = [tree]
        return agg

    # --- importlib.metadata fallback ---

    def _parse_with_importlib(self, python_exe: str) -> DepNode:
        """使用 importlib.metadata 分析已安装包依赖（fallback）。"""
        try:
            from importlib.metadata import distributions, requires
        except ImportError:
            return DepNode(name="error", type=NodeType.EXTERNAL,
                           detail="importlib.metadata unavailable (Python <3.8)")

        # 收集所有已安装包
        all_pkgs: Dict[str, str] = {}
        for dist in distributions():
            name = dist.metadata.get("Name")
            if name:
                all_pkgs[name.lower()] = dist.version

        visited: Set[str] = set()

        def parse_deps(pkg_name: str, depth: int = 0) -> DepNode:
            name_lower = pkg_name.lower()
            ver = all_pkgs.get(name_lower, "")
            detail = "v%s" % ver if ver else "(not installed)"

            if name_lower in visited or depth >= 10:
                if name_lower in visited:
                    return DepNode(name=pkg_name, type=NodeType.CIRCULAR,
                                   detail="%s (循环)" % detail)
                return DepNode(name=pkg_name, type=NodeType.PKG, detail=detail)

            visited.add(name_lower)
            node = DepNode(name=pkg_name, type=NodeType.PKG, detail=detail)

            try:
                reqs = requires(pkg_name) or []
            except Exception:
                reqs = []

            for req in reqs:
                # 解析 requirement 字符串: "pkg [extra] >=1.0; python_version<'3.9'"
                dep_name = re.split(r'[<>=!~\s\[]', req)[0].strip()
                if not dep_name:
                    continue
                # 跳过带 marker 的可选依赖（简化处理）
                if ";" in req and "extra ==" in req:
                    continue
                child = parse_deps(dep_name, depth + 1)
                node.children.append(child)

            visited.discard(name_lower)
            return node

        if self.package_name:
            tree = parse_deps(self.package_name)
        else:
            tree = DepNode(name="installed-packages", type=NodeType.PKG,
                           detail="%d packages (importlib fallback)" % len(all_pkgs))
            for name, ver in sorted(all_pkgs.items()):
                tree.children.append(DepNode(name=name, type=NodeType.PKG,
                                              detail="v%s" % ver))

        agg = DepNode(name="Package Dependencies (importlib)", type=NodeType.PKG, detail="")
        agg.children = [tree]
        return agg


# ============================================================
# 调用图后端抽象（Strategy 模式）
# ============================================================

class CallGraphBackend(ABC):
    """产生 CallGraphDB 的后端接口（Strategy 模式）。"""

    @abstractmethod
    def parse(self, path: str) -> Optional[CallGraphDB]:
        """解析 Python 源文件，返回调用图；失败返回 None。"""


# ============================================================
# 调用图后端 1: code2flow（业界标准，调用图+流程图混合，支持纯脚本）
# ============================================================

class Code2flowBackend(CallGraphBackend):
    """通过 code2flow 生成调用图+流程图混合视图。

    code2flow 将全局代码作为 ``(global)`` 节点，因此纯脚本文件也能产出
    有意义的调用图。节点 label 包含行号和函数签名。
    输出 JSON 格式：graph.nodes（uid→{label,name}）+ graph.edges（source→target）。
    """

    def parse(self, path: str) -> Optional[CallGraphDB]:
        exe = self._find_executable()
        if not exe:
            logger.debug("code2flow executable not found")
            return None

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(tmp_fd)
        try:
            cmd = [exe, path, "--output", tmp_path, "--quiet", "--no-trimming"]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                errors="replace",
            )
            if result.returncode != 0:
                logger.warning("code2flow exited with code %d: %s",
                               result.returncode, result.stderr.strip()[:300])
                return None
            if not os.path.exists(tmp_path):
                logger.warning("code2flow produced no output file")
                return None
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._parse_json(data)
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as e:
            logger.warning("code2flow failed: %s", e)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _find_executable() -> Optional[str]:
        """定位 code2flow 可执行文件。"""
        found = shutil.which("code2flow")
        if found:
            return found
        # 从当前 Python 推导 Scripts 目录
        scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
        candidate = os.path.join(scripts_dir, "code2flow.exe")
        if os.path.exists(candidate):
            return candidate
        return None

    @staticmethod
    def _parse_json(data: dict) -> Optional[CallGraphDB]:
        graph = data.get("graph", {})
        nodes = graph.get("nodes", {})
        edges = graph.get("edges", [])
        if not nodes:
            return None

        db = CallGraphDB()

        for uid, node in nodes.items():
            name = node.get("name", "")      # e.g. "dep_tree::main" / "dep_tree::(global)"
            label = node.get("label", "")    # e.g. "1879: main()"

            # 显示名：取最后一个 :: 之后的部分
            parts = name.split("::")
            display = parts[-1] if parts else name
            if display == "(global)":
                display = "(global script)"

            db.labels[uid] = display

            # 从 label 提取行号："1879: main()" → "line 1879"
            loc_m = re.match(r"(\d+):", label)
            if loc_m:
                db.locations[uid] = "line %s" % loc_m.group(1)

            # 入口点：全局代码 或 顶层函数（name 中只有一个 ::）
            if "(global)" in name or name.count("::") == 1:
                if uid not in db.entry_points:
                    db.entry_points.append(uid)

        for edge in edges:
            source = edge.get("source")
            target = edge.get("target")
            if source in db.labels and target in db.labels:
                db.calls.setdefault(source, []).append(target)

        if not db.calls and not db.labels:
            return None
        return db


# ============================================================
# 调用图后端 2: pyan3（业界标准静态调用图分析）
# ============================================================

class Pyan3Backend(CallGraphBackend):
    """通过 pyan3 静态分析获取函数调用图。

    调用 `pyan3 --dot --no-defines`，解析 DOT 输出中的节点（tooltip 含类型信息）
    和调用边。只保留函数/方法节点，过滤类、模块、属性节点。
    """

    def parse(self, path: str) -> Optional[CallGraphDB]:
        python_exe = resolve_python_exe(None, path)
        cmd = [python_exe, "-m", "pyan", path, "--dot", "--no-defines"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                errors="replace",
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("pyan3 execution failed: %s", e)
            return None

        if result.returncode != 0:
            logger.warning("pyan3 exited with code %d: %s",
                           result.returncode, result.stderr.strip()[:300])
            return None

        return self._parse_dot(result.stdout)

    @staticmethod
    def _parse_dot(dot_text: str) -> Optional[CallGraphDB]:
        """解析 pyan3 DOT 输出，提取函数/方法节点和调用边。"""
        db = CallGraphDB()

        # 节点行: "node_id" [label="...", tooltip="full.name\nfile:line\ntype info"];
        node_re = re.compile(r'"([^"]+)"\s*\[([^\]]*)\];')
        for m in node_re.finditer(dot_text):
            node_id = m.group(1)
            attrs = m.group(2)
            tooltip_m = re.search(r'tooltip="([^"]*)"', attrs)
            label_m = re.search(r'label="([^"]*)"', attrs)
            label = label_m.group(1) if label_m else node_id

            if not tooltip_m:
                continue
            tooltip = tooltip_m.group(1)
            parts = tooltip.split("\\n")
            location = parts[1] if len(parts) > 1 else ""
            type_info = parts[2] if len(parts) > 2 else ""

            # 只保留函数和方法（tooltip 最后一行含 "function in" 或 "method in"）
            if "function in" in type_info:
                db.labels[node_id] = label
                db.locations[node_id] = location
                db.entry_points.append(node_id)
            elif "method in" in type_info:
                db.labels[node_id] = label
                db.locations[node_id] = location

        # 边行: "caller" -> "callee" [style="solid", color="#000000"];
        edge_re = re.compile(r'"([^"]+)"\s*->\s*"([^"]+)"')
        for m in edge_re.finditer(dot_text):
            caller, callee = m.group(1), m.group(2)
            # 只保留函数/方法之间的调用边
            if caller in db.labels and callee in db.labels:
                db.calls.setdefault(caller, []).append(callee)

        if not db.calls and not db.labels:
            return None
        return db


# ============================================================
# 调用图后端 3: 纯 ast 静态分析（fallback，功能有限）
# ============================================================

class AstCallGraphBackend(CallGraphBackend):
    """纯 ast 静态分析 fallback。只处理直接函数调用和 self.method()。"""

    def parse(self, path: str) -> Optional[CallGraphDB]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                tree = ast.parse(f.read(), filename=path)
        except (SyntaxError, IOError, OSError) as e:
            logger.warning("ast parse failed: %s", e)
            return None

        db = CallGraphDB()
        module_name = os.path.splitext(os.path.basename(path))[0]

        # 建立 parent 引用
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node  # type: ignore[attr-defined]

        # 收集所有函数定义（顶层函数和类方法）
        functions: Dict[str, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                parts = [node.name]
                parent = getattr(node, "parent", None)
                while parent is not None:
                    if isinstance(parent, ast.ClassDef):
                        parts.insert(0, parent.name)
                    elif isinstance(parent, ast.Module):
                        break
                    parent = getattr(parent, "parent", None)
                full_name = ".".join(parts)
                node_id = "%s__%s" % (module_name, full_name.replace(".", "__"))
                functions[node_id] = node
                db.labels[node_id] = full_name
                db.locations[node_id] = "%s:%d" % (path, node.lineno)
                if len(parts) == 1:
                    db.entry_points.append(node_id)

        # 分析每个函数体内的调用
        for node_id, func_node in functions.items():
            callees: List[str] = []
            for call in ast.walk(func_node):
                if not isinstance(call, ast.Call):
                    continue
                callee_name = self._resolve_call_name(call, functions, module_name)
                if callee_name and callee_name in functions and callee_name != node_id:
                    callees.append(callee_name)
            if callees:
                db.calls[node_id] = list(dict.fromkeys(callees))

        if not db.calls and not db.labels:
            return None
        return db

    @staticmethod
    def _resolve_call_name(call: ast.Call, functions: Dict[str, ast.FunctionDef],
                           module_name: str) -> Optional[str]:
        """从 ast.Call 节点解析被调用函数的限定名。"""
        func = call.func
        if isinstance(func, ast.Name):
            candidate = "%s__%s" % (module_name, func.id)
            if candidate in functions:
                return candidate
            return None
        if isinstance(func, ast.Attribute):
            method_name = func.attr
            for fid in functions:
                if fid.endswith("__" + method_name):
                    return fid
            return None
        return None


# ============================================================
# 调用图构建器：从 CallGraphDB 展开为 DepNode 树
# ============================================================

class CallGraphBuilder:
    """从 CallGraphDB 递归构建 DepNode 树（从入口函数展开调用链）。"""

    def __init__(self, db: CallGraphDB, max_depth: int = 10) -> None:
        self.db = db
        self.max_depth = max_depth

    def build(self, entry: str) -> DepNode:
        return self._build(entry, set(), set(), 0)

    def _build(self, node_id: str, seen: Set[str], expanded: Set[str],
               depth: int) -> DepNode:
        label = self.db.labels.get(node_id, node_id)
        location = self.db.locations.get(node_id, "")

        if node_id in seen:
            return DepNode(name=label, type=NodeType.CIRCULAR,
                           detail="%s (循环调用)" % location)
        if node_id in expanded or depth >= self.max_depth:
            return DepNode(name=label, type=NodeType.REF,
                           detail="%s (见上方)" % location)

        node = DepNode(name=label, type=NodeType.FUNCTION, detail=location)
        seen = seen | {node_id}
        expanded.add(node_id)

        for callee in self.db.calls.get(node_id, []):
            node.children.append(self._build(callee, seen, expanded, depth + 1))

        return node


# ============================================================
# 调用图解析器（Facade：后端策略链 + 构建图）
# ============================================================

@register_parser
class CallGraphParser(Parser):
    """Python 函数调用图解析器。

    Facade：按顺序尝试 backends 列表，第一个成功的使用。
    不注册扩展名（extensions=[]），通过 --mode callgraph 显式选择。
    """

    extensions: List[str] = []

    def __init__(self, backends: List[CallGraphBackend], max_depth: int = 10) -> None:
        self.backends = backends
        self.max_depth = max_depth

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "CallGraphParser":
        backends: List[CallGraphBackend] = []
        if not getattr(args, "no_code2flow", False):
            backends.append(Code2flowBackend())
        if not getattr(args, "no_pyan3", False):
            backends.append(Pyan3Backend())
        backends.append(AstCallGraphBackend())
        return cls(backends=backends, max_depth=args.max_depth)

    @property
    def mode_label(self) -> str:
        return "Call Graph Mode"

    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        abs_path = os.path.abspath(path)

        # 策略链：按顺序尝试后端
        db: Optional[CallGraphDB] = None
        for backend in self.backends:
            db = backend.parse(abs_path)
            if db is not None:
                logger.debug("Using call graph backend: %s", type(backend).__name__)
                break

        if db is None:
            logger.warning("All call graph backends failed, using empty graph")
            db = CallGraphDB()

        # 确定入口函数
        entry = root
        if entry is None:
            for ep in db.entry_points:
                if db.labels.get(ep, "").lower() == "main":
                    entry = ep
                    break
            if entry is None and db.entry_points:
                entry = db.entry_points[0]
            if entry is None and db.labels:
                entry = next(iter(db.labels))

        if entry is None:
            agg = DepNode(name=os.path.basename(path), type=NodeType.PY_MODULE,
                          detail=abs_path)
            agg.children = [DepNode(name="(no functions found)",
                                    type=NodeType.EXTERNAL)]
            return agg

        builder = CallGraphBuilder(db, max_depth=self.max_depth)
        tree = builder.build(entry)

        agg = DepNode(name=os.path.basename(path), type=NodeType.PY_MODULE,
                      detail=abs_path)
        agg.children = [tree]
        return agg


# ============================================================
# 渲染器（Strategy 模式：Renderer 抽象 + 具体实现）
# ============================================================

class Renderer(ABC):
    """依赖树渲染器抽象接口。"""

    @abstractmethod
    def render(self, node: DepNode) -> str:
        ...


class TextRenderer(Renderer):
    """ASCII 文本树渲染（默认输出）。"""

    def __init__(self, path: str = "", mode_label: str = "") -> None:
        self.path = path
        self.mode_label = mode_label

    def render(self, node: DepNode) -> str:
        lines: List[str] = ["=" * 70]
        header = "  依赖树  (%s)" % self.path
        if self.mode_label:
            header += "  [%s]" % self.mode_label
        lines.append(header)
        lines.append("=" * 70)
        self._walk(node, "", True, lines)
        lines.append("=" * 70)
        return "\n".join(lines)

    def _walk(self, node: DepNode, prefix: str, is_last: bool, lines: List[str]) -> None:
        conn = "└── " if is_last else "├── "
        label = node.name + ("   [%s]" % node.detail if node.detail else "")
        tag = node.type.tag
        lines.append("%s%s %s %s" % (prefix, conn, label, tag))
        for i, c in enumerate(node.children):
            self._walk(c, prefix + ("    " if is_last else "│   "),
                       i == len(node.children) - 1, lines)


class JsonRenderer(Renderer):
    """JSON 渲染。"""

    def render(self, node: DepNode) -> str:
        return json.dumps(node.to_dict(), indent=2, ensure_ascii=False)


class DotRenderer(Renderer):
    """Graphviz DOT 渲染。"""

    def render(self, node: DepNode) -> str:
        ids: Dict[int, str] = {}
        lines: List[str] = []
        counter = [0]

        def emit(n: DepNode) -> str:
            key = id(n)
            if key not in ids:
                counter[0] += 1
                ids[key] = "n%d" % counter[0]
                lines.append('  %s [label="%s\\n%s", shape=%s, color="%s"];' % (
                    ids[key], n.name.replace('"', "'"), n.detail,
                    n.type.dot_shape, n.type.dot_color))
            return ids[key]

        def walk(n: DepNode) -> str:
            nid = emit(n)
            for c in n.children:
                lines.append("  %s -> %s;" % (nid, walk(c)))
            return nid

        walk(node)
        return "digraph deps {\n  rankdir=LR;\n" + "\n".join(lines) + "\n}\n"


class MermaidRenderer(Renderer):
    """Mermaid flowchart 渲染（适合 Markdown/网页渲染）。"""

    def render(self, node: DepNode) -> str:
        lines: List[str] = ["graph TD"]
        counter = [0]

        def walk(n: DepNode, parent_id: Optional[str] = None) -> str:
            counter[0] += 1
            nid = "n%d" % counter[0]
            label = n.name.replace('"', "'")
            open_s, close_s = n.type.mermaid_shape
            lines.append("  %s%s%s%s" % (nid, open_s, label, close_s))
            if parent_id:
                lines.append("  %s --> %s" % (parent_id, nid))
            for c in n.children:
                walk(c, nid)
            return nid

        walk(node)
        return "\n".join(lines) + "\n"


# ============================================================
# CLI
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    ap = argparse.ArgumentParser(
        description="通用依赖树解析工具（Makefile / Python源码 / Python包）")
    ap.add_argument("target", nargs="?", help="目标文件路径（Makefile/*.py/requirements.txt 等）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--dot", action="store_true", help="输出 Graphviz DOT")
    ap.add_argument("--mermaid", action="store_true", help="输出 Mermaid flowchart")
    ap.add_argument("--list-parsers", action="store_true", help="列出已注册的解析器")
    ap.add_argument("--no-make", action="store_true", help="禁用 make -pn 后端，强制纯 Python 解析")
    ap.add_argument("--root", help="从指定目标开始展开（默认用 Makefile 的 default goal）")
    ap.add_argument("--dirty", action="store_true", help="标记需要重建的节点（运行 make -nd 检测）")
    # Python 模式新增参数
    ap.add_argument("--mode", choices=["auto", "makefile", "source", "package", "callgraph"],
                    default="auto", help="解析模式（默认 auto: 按文件扩展名自动推断）")
    ap.add_argument("--name", help="包模式: 指定要分析的包名（不指定则分析全部）")
    ap.add_argument("--python", help="指定 Python 可执行文件路径（默认自动检测项目 venv）")
    ap.add_argument("--reverse", action="store_true", help="包模式: 逆向依赖（谁依赖了我）")
    ap.add_argument("--no-stdlib", action="store_true", help="源码模式: 隐藏标准库节点")
    ap.add_argument("--max-depth", type=int, default=10, help="源码模式: 最大递归深度（默认10）")
    ap.add_argument("--no-pydeps", action="store_true", help="源码模式: 禁用 pydeps，强制 ast fallback")
    ap.add_argument("--no-pipdeptree", action="store_true",
                    help="包模式: 禁用 pipdeptree，强制 importlib.metadata fallback")
    ap.add_argument("--no-pyan3", action="store_true",
                    help="调用图模式: 禁用 pyan3，强制 ast fallback")
    ap.add_argument("--no-code2flow", action="store_true",
                    help="调用图模式: 禁用 code2flow 后端")
    ap.add_argument("--render", choices=["png", "svg"], default=None,
                    help="渲染 DOT 为图片（需 graphviz dot），输出到同目录下")
    ap.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = ap.parse_args(argv)

    if args.verbose:
        logging.getLogger("dep_tree").setLevel(logging.DEBUG)

    registry = DEFAULT_REGISTRY

    if args.list_parsers:
        print("已注册的解析器:")
        for cls in sorted(registry.all_parsers(), key=lambda c: c.__name__):
            exts = sorted(cls.extensions)
            print("  - %s  (扩展名: %s)" % (cls.__name__, ", ".join(exts)))
        return 0

    # 包模式可以没有 target 文件（用当前目录做 venv 检测）
    if not args.target and args.mode != "package":
        ap.print_help()
        return 1

    path = os.path.abspath(args.target) if args.target else os.getcwd()
    if args.target and not os.path.exists(path):
        print("错误: 文件不存在: %s" % path, file=sys.stderr)
        return 1

    # 模式选择: --mode 优先，否则按扩展名自动推断
    if args.mode == "makefile":
        parser_cls = MakefileParser
    elif args.mode == "source":
        parser_cls = PythonSourceParser
    elif args.mode == "package":
        parser_cls = PythonPackageParser
    elif args.mode == "callgraph":
        parser_cls = CallGraphParser
    else:
        parser_cls = registry.find(path)
        if parser_cls is None:
            print("错误: 未找到可处理 %s 的解析器（可用 --mode 显式指定）" % path,
                  file=sys.stderr)
            return 1

    # OCP：每个解析器自己处理参数映射
    parser = parser_cls.from_args(args)
    tree = parser.parse(path, root=args.root)

    # 选择渲染器（Strategy）
    if args.json:
        renderer: Renderer = JsonRenderer()
    elif args.dot:
        renderer = DotRenderer()
    elif args.mermaid:
        renderer = MermaidRenderer()
    else:
        renderer = TextRenderer(path=path, mode_label=parser.mode_label)

    output = renderer.render(tree)

    # DOT 模式支持 --render 渲染图片
    if args.dot and args.render:
        output_path = _render_dot(output, path, args.render)
        if output_path:
            print("Rendered: %s" % output_path)
            return 0
        # 渲染失败时回退到输出 DOT 文本

    print(output)
    return 0


def _render_dot(dot_content: str, source_path: str, fmt: str) -> Optional[str]:
    """将 DOT 内容通过 graphviz dot 渲染为图片。

    返回输出文件路径，失败返回 None。
    """
    dot_exe = shutil.which("dot")
    if not dot_exe:
        print("错误: 未找到 graphviz 'dot' 命令，请先安装 Graphviz", file=sys.stderr)
        return None

    base_dir = os.path.dirname(os.path.abspath(source_path))
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    output_file = os.path.join(base_dir, "%s-deps.%s" % (base_name, fmt))

    try:
        result = subprocess.run(
            [dot_exe, "-T%s" % fmt, "-o", output_file],
            input=dot_content, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print("错误: dot 渲染失败: %s" % e, file=sys.stderr)
        return None

    if result.returncode != 0:
        print("错误: dot 退出码 %d: %s" % (result.returncode, result.stderr[:300]),
              file=sys.stderr)
        return None

    return output_file


if __name__ == "__main__":
    sys.exit(main())
