#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dep_tree.py — 通用构建/脚本依赖树解析工具。

对目标文件（Makefile / Python 脚本 / CMD 批处理）做依赖展开，输出完整依赖树。
设计为可扩展：解析器以"注册表 + 插件"方式组织，新增文件类型只需实现一个
Parser 子类并注册，无需改动调度逻辑。

用法（任意工作目录均可调用）：
    python dep_tree.py <目标文件路径>              # 文本树
    python dep_tree.py <目标文件路径> --json        # JSON
    python dep_tree.py <目标文件路径> --dot         # Graphviz DOT
    python dep_tree.py <目标文件路径> --list-parsers # 列出已注册的解析器

支持的文件类型（按扩展名自动识别）：
    - Makefile / *.mk / *.mak      → MakefileParser
    - *.py                         → PythonParser（已预留，待实现 import 解析）
    - *.bat / *.cmd                → CMDParser（已预留，待实现 call/引用解析）

设计要点：
    - 解析器接口统一为 Parser 抽象基类（见下方）。
    - 统一依赖树节点结构 DepNode（dataclass），各解析器产出同构数据。
    - 通过 PARSERS 注册表按扩展名路由，新增解析器 = 新增一个类 + 注册一行。
"""

from __future__ import annotations

import os
import re
import sys
import json
import argparse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Type


# ============================================================
# 统一数据结构
# ============================================================

@dataclass
class DepNode:
    """依赖树节点（各解析器统一产出的数据结构）。"""
    name: str                        # 节点名（目标名 / 文件路径 / 脚本名）
    type: str = "target"             # target | file | script | submake | circular
    detail: str = ""                 # 附加说明（如"子工程 Makefile: xxx/Makefile"）
    children: List["DepNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
        }


# ============================================================
# 解析器抽象接口
# ============================================================

class Parser(ABC):
    """依赖解析器抽象基类。

    子类需实现：
      - extensions: 类属性，支持的文件扩展名列表（不含点，如 ["mk", "mak"]）
      - parse(path, root) -> DepNode: 解析单个文件，返回依赖树根节点
        （root 为可选的目标名，缺省时解析文件内所有目标）

    可选覆盖：
      - matches(path) -> bool：默认按 extensions 匹配，可覆盖实现更精细的判断。
    """

    extensions: List[str] = []

    @classmethod
    def matches(cls, path: str) -> bool:
        """判断该解析器是否能处理此文件（默认按扩展名）。"""
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        name = os.path.basename(path).lower()
        return ext in cls.extensions or name in cls.extensions

    @abstractmethod
    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        """解析文件，返回依赖树根节点。"""


# ============================================================
# 解析器注册表
# ============================================================

PARSERS: Dict[str, Type[Parser]] = {}


def register_parser(parser_cls: Type[Parser]) -> Type[Parser]:
    """注册解析器（按扩展名 / 文件名关键字）。"""
    for key in parser_cls.extensions:
        PARSERS[key] = parser_cls
    return parser_cls


def find_parser(path: str) -> Optional[Type[Parser]]:
    """按文件路径找到匹配的解析器类，找不到返回 None。"""
    for parser_cls in PARSERS.values():
        if parser_cls.matches(path):
            return parser_cls
    return None


# ============================================================
# Makefile 解析器
# ============================================================

@register_parser
class MakefileParser(Parser):
    """解析 Makefile 依赖关系。

    识别三类依赖：
      - 目标间依赖（target: dep1 dep2）
      - 子工程递归 make（$(MAKE) -C <dir>）
      - 脚本调用（$(SCRIPTS_DIR)/xxx.py 等）
    """

    extensions = ["makefile", "mk", "mak", "makfile"]

    # 解析规则
    _VAR_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(.*)$')
    _TARGET_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_.-]*)\s*:\s*(.*)$')
    _SCRIPT_REF_RE = re.compile(r'\$\((\w+)\)/([\w.]+)')
    _SUBMAKE_RE = re.compile(r'\$\(MAKE\)\s+-C\s+\$?\(?([\w/]+)\)?')
    _PYVAR_RE = re.compile(r'\$\(([A-Za-z_][A-Za-z0-9_]*)\)')
    _PHONY_RE = re.compile(r'^\.PHONY\s*:\s*(.+)$')

    def parse(self, path: str, root: Optional[str] = None) -> DepNode:
        self._path = path
        variables, targets, phony = self._parse_file(path)
        roots = [root] if root else (phony or list(targets.keys()))
        trees = [self._build_tree(t, variables, targets) for t in roots if t in targets]

        if root and root in targets:
            return trees[0]
        # 无 root 时，返回一个虚拟根聚合所有目标
        agg = DepNode(name=os.path.basename(path), type="file",
                      detail=os.path.abspath(path))
        agg.children = trees
        return agg

    # ---- 内部实现 ----
    def _parse_file(self, path: str):
        variables: Dict[str, str] = {}
        targets: Dict[str, Dict] = {}
        phony: List[str] = []
        cur_target: Optional[str] = None

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                code = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""

                m = self._PHONY_RE.match(line.strip())
                if m:
                    phony = [x for x in m.group(1).split() if x]
                    cur_target = None
                    continue

                m_var = self._VAR_RE.match(code) if code else None
                if m_var and not line.startswith("\t"):
                    variables[m_var.group(1)] = m_var.group(2).strip()
                    cur_target = None
                    continue

                m_tgt = self._TARGET_RE.match(code) if code else None
                if m_tgt and not line.startswith("\t"):
                    cur_target = m_tgt.group(1)
                    deps = [d for d in m_tgt.group(2).split() if d]
                    targets[cur_target] = {"deps": deps, "recipe": []}
                    continue

                if line.startswith("\t") and cur_target:
                    targets[cur_target]["recipe"].append(line.strip())

        return variables, targets, phony

    def _expand(self, value: str, variables: Dict[str, str], _depth: int = 0) -> str:
        if _depth > 8:
            return value
        def repl(m):
            name = m.group(1)
            return self._expand(variables[name], variables, _depth + 1) if name in variables else m.group(0)
        return self._PYVAR_RE.sub(repl, value)

    def _build_tree(self, target: str, variables: Dict[str, str],
                    targets: Dict[str, Dict], seen=None) -> DepNode:
        if seen is None:
            seen = set()
        if target in seen:
            return DepNode(name=target, type="circular")
        seen = seen | {target}

        node = DepNode(name=target)
        if target not in targets:
            node.name = self._expand(target, variables)
            node.type = "file"
            node.detail = "(外部依赖)"
            return node

        info = targets[target]
        for dep in info["deps"]:
            node.children.append(self._build_tree(dep, variables, targets, seen))

        for cmd in info["recipe"]:
            for sm in self._SUBMAKE_RE.finditer(cmd):
                varname = sm.group(1)
                subdir = self._expand("$(%s)" % varname, variables) if varname in variables else varname
                child = DepNode(name="make -C %s" % subdir, type="submake",
                                detail="子工程 Makefile: %s/Makefile" % subdir)
                base = os.path.dirname(os.path.abspath(self._path))
                mkfile = os.path.join(base, subdir, "Makefile")
                if os.path.exists(mkfile):
                    child.children.append(DepNode(
                        name=os.path.relpath(mkfile, base).replace(os.sep, "/"),
                        type="file", detail="子构建配置"))
                node.children.append(child)

            for sc in self._SCRIPT_REF_RE.finditer(cmd):
                sdir = variables.get(sc.group(1), "scripts")
                sp = os.path.join(self._expand(sdir, variables), sc.group(2)).replace("\\", "/")
                node.children.append(DepNode(name=sp, type="script", detail="脚本依赖"))

        return node


# ============================================================
# 预留解析器（后续实现）
# ============================================================

# @register_parser
# class PythonParser(Parser):
#     """解析 Python 脚本的 import 依赖（待实现）。"""
#     extensions = ["py"]
#     def parse(self, path, root=None):
#         # TODO: 解析 import / from ... import 语句，追踪本地模块
#         raise NotImplementedError("PythonParser 尚未实现")


# @register_parser
# class CMDParser(Parser):
#     """解析 CMD 批处理的 call / 引用依赖（待实现）。"""
#     extensions = ["bat", "cmd"]
#     def parse(self, path, root=None):
#         # TODO: 解析 call / start / 变量引用
#         raise NotImplementedError("CMDParser 尚未实现")


# ============================================================
# 渲染
# ============================================================

def render(node: DepNode, prefix: str = "", is_last: bool = True, lines=None) -> List[str]:
    if lines is None:
        lines = []
    conn = "└── " if is_last else "├── "
    label = node.name
    if node.detail:
        label += "   [%s]" % node.detail
    tag = {"target": "", "file": "(file)", "script": "(script)",
           "submake": "(submake)", "circular": "(循环!)"}.get(node.type, "")
    lines.append("%s%s %s %s" % (prefix, conn, label, tag))
    kids = node.children
    for i, c in enumerate(kids):
        render(c, prefix + ("    " if is_last else "│   "), i == len(kids) - 1, lines)
    return lines


def to_dot(node: DepNode) -> str:
    """转 Graphviz DOT（同名节点去重）。"""
    ids: Dict[str, str] = {}
    lines: List[str] = []

    def emit(name, detail, ntype):
        if name not in ids:
            ids[name] = "n%d" % len(ids)
            shape = "box" if ntype in ("file", "script") else "ellipse"
            color = ("#cc6600" if ntype == "script"
                     else "#336699" if ntype == "file" else "#333333")
            lines.append('  %s [label="%s\\n%s", shape=%s, color="%s"];' % (
                ids[name], name.replace('"', "'"), detail, shape, color))
        return ids[name]

    def walk(n):
        nid = emit(n.name, n.detail, n.type)
        for c in n.children:
            cid = walk(c)
            lines.append('  %s -> %s;' % (nid, cid))
        return nid

    walk(node)
    return "digraph deps {\n  rankdir=LR;\n" + "\n".join(lines) + "\n}\n"


# ============================================================
# CLI
# ============================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="通用构建/脚本依赖树解析工具（支持 Makefile，预留 Python/CMD）")
    ap.add_argument("target", nargs="?", help="目标文件路径（Makefile/*.mk/*.py/*.bat 等）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--dot", action="store_true", help="输出 Graphviz DOT")
    ap.add_argument("--list-parsers", action="store_true", help="列出已注册的解析器")
    args = ap.parse_args(argv)

    if args.list_parsers:
        print("已注册的解析器:")
        for cls in sorted({p.__name__ for p in PARSERS.values()}):
            print("  - %s  (扩展名: %s)" % (cls, ", ".join(
                sorted({p for p2 in PARSERS.values() if p2.__name__ == cls
                        for p in p2.extensions}))))
        return 0

    if not args.target:
        ap.print_help()
        return 1

    path = os.path.abspath(args.target)
    if not os.path.exists(path):
        print("错误: 文件不存在: %s" % path, file=sys.stderr)
        return 1

    parser_cls = find_parser(path)
    if parser_cls is None:
        print("错误: 未找到可处理 %s 的解析器" % path, file=sys.stderr)
        print("      已支持: %s" % ", ".join(sorted(
            {e for p in PARSERS.values() for e in p.extensions})), file=sys.stderr)
        return 1

    parser = parser_cls()
    tree = parser.parse(path)

    if args.json:
        print(json.dumps(tree.to_dict(), indent=2, ensure_ascii=False))
    elif args.dot:
        print(to_dot(tree))
    else:
        print("=" * 70)
        print("  依赖树  (%s)" % path)
        print("=" * 70)
        for line in render(tree, "", True):
            print(line)
        print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
