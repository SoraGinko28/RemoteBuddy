"""一场会：一个 store、一条会议记录、一叠事先设计好的工单、桌上几个成员。

harness 只有三件事，这里都摆着：

1. **一个 store**（`Store`）：共享的那一份数据，值按数据路径放，会议记录也在里头；
2. **一个"该发哪一张"的判据**（`Meeting.due` / `Meeting.next_ticket`）：读 store 算出来的——要读的
   都在了、它自己那几步还没答，就该发；
3. **几张能力表**（每个 `Member` 一张）：谁会不会哪个算子——表在成员自己的进程里（`member.py`）。

发一张工单（`Meeting.publish`）：把它做成一个包（`store/` 是这一步要读的值；**程序不发副本**，
发给会上的**每个**成员，各人一份自己的副本。跑是成员那一侧的事：`--bundle` 指到哪一份，就是发给谁的。
答下的值由成员抄回会议；每个成员包里那份副本留着：**对账靠它们**。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast

from .host import Member, capability_digest
from .prepare import call_of
from .store import Store
from .workflow import Workflow, addresses_of_call

CASE_PREFIX = "case"
TEMPLATE = "_ticket"
STORE_DIR = "store"


class Meeting:
    """One meeting: its store, its tickets, and the members sitting at it."""

    def __init__(self, root, workflow: Workflow, record_path=None):
        self.root = Path(root)
        self.workflow = workflow
        self.store = Store(self.root / STORE_DIR, record_path or self.root / "meeting_record.md")
        self.tickets_dir = self.root / "tickets"
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self.members: Dict[str, Member] = {}

    # ------------------------------------------------------------------
    # Who is at the meeting
    # ------------------------------------------------------------------

    def join(self, member: Member) -> None:
        """One member sits down."""
        self.members[member.name] = member

    # ------------------------------------------------------------------
    # Cases and data paths
    # ------------------------------------------------------------------

    def case(self, name: str) -> str:
        """A case's data path: the case itself."""
        return f"{CASE_PREFIX}/{name}"

    def absolute(self, case: str, module_path: str) -> str:
        """A ticket's data path, made absolute: a ticket names no case."""
        if not module_path:
            return case
        if module_path == case or module_path.startswith(f"{case}/"):
            return module_path
        return f"{case}/{module_path}"

    # ------------------------------------------------------------------
    # Which ticket is due
    # ------------------------------------------------------------------

    def due(self, ticket: str, case: str) -> Tuple[bool, str]:
        """Should this ticket go out now? Read the store, not a fixed order."""
        for module_path, name in self.workflow.addresses_in(ticket):
            wanted = self.absolute(case, module_path)
            if not self.store.answered(wanted, name):
                return False, f"还缺 {wanted} + {name}"
        for module_path, name in self.workflow.answers_of(ticket):
            done = self.absolute(case, module_path)
            if self.store.answered(done, name):
                return False, f"{done} + {name} 已经答过了"
        return True, "要读的都在，自己还没做过"

    def next_ticket(self, case: str) -> Optional[str]:
        """The first ticket that is due, or None when the meeting has run out."""
        for ticket in self.workflow.tickets():
            wanted, _why = self.due(ticket, case)
            if wanted:
                return ticket
        return None

    # ------------------------------------------------------------------
    # Issuing one ticket: a package for every member
    # ------------------------------------------------------------------

    def publish(self, ticket: str, case: str) -> Dict[str, Path]:
        """把这一张发给会上的每个成员：一张工单一个包，成员各拿一份自己的副本。

        返回"发给谁 → 它那一包在哪"——成员进程就是拿这个路径跑的。
        """
        if not any("这份流程：" in line for line in self.store.records()):
            # 内容寻址：这一场会跑的是哪份流程、哪一版这一侧的代码，都记在记录的第一行
            # （一场会只记一次：发单的那一侧每张工单都会新建一个 Meeting）。
            self.store.record(f"- 这份流程：{self.workflow.digest()}"
                              f"（这一侧的代码：{capability_digest()}）")
        template = self.tickets_dir / ticket / TEMPLATE
        self._bundle(template, ticket, case)
        bundles: Dict[str, Path] = {}
        self.store.record(f"- 工单 {ticket}（案子 {case}）")
        for member in self.members.values():
            bundle = self.tickets_dir / ticket / member.name
            if bundle.exists():
                shutil.rmtree(bundle)
            shutil.copytree(template, bundle)
            bundles[member.name] = bundle
        shutil.rmtree(template)
        return bundles

    # ------------------------------------------------------------------
    # The package: the data this step reads (the program stays in the norm)
    # ------------------------------------------------------------------

    def _bundle(self, bundle: Path, ticket: str, case: str) -> None:
        """`store/` is the data this step reads. The program is not copied: the norm has it."""
        if bundle.exists():
            shutil.rmtree(bundle)
        (bundle / STORE_DIR).mkdir(parents=True, exist_ok=True)
        seen: set = set()
        self._slice(bundle / STORE_DIR, ticket, case, seen)

    def _slice(self, store_root: Path, ticket: str, case: str, seen: set) -> None:
        """Copy what the ticket reads, what it answers, and what those steps read."""
        wanted = list(self.workflow.addresses_in(ticket)) + list(self.workflow.answers_of(ticket))
        for module_path, name in wanted:
            absolute = self.absolute(case, module_path)
            if (absolute, name) in seen:
                continue
            seen.add((absolute, name))
            prepared = self.store.prepared(absolute, name)
            if prepared is None:
                continue
            self.store.copy_stored(store_root, [(absolute, name)])
            self._slice_calls(store_root, call_of(prepared), case, seen)

    def _slice_calls(self, store_root: Path, call, case: str, seen: set) -> None:
        for module_path, name in addresses_of_call(call):
            absolute = self.absolute(case, module_path)
            if (absolute, name) in seen:
                continue
            seen.add((absolute, name))
            prepared = self.store.prepared(absolute, name)
            if prepared is None:
                continue
            self.store.copy_stored(store_root, [(absolute, name)])
            self._slice_calls(store_root, call_of(prepared), case, seen)

    # ------------------------------------------------------------------
    # Reconciliation: every member kept a copy
    # ------------------------------------------------------------------

    def backups(self, data_path: str, name: str) -> List[Tuple[str, str, Any]]:
        """Every bundle copy of one stored value: (ticket, member, what it says)."""
        out: List[Tuple[str, str, Any]] = []
        for member_dir in sorted(self.tickets_dir.glob(f"*/*")):
            if member_dir.name == TEMPLATE:
                continue
            value, answered = Store(member_dir / STORE_DIR).read(data_path, name)
            if answered:
                out.append((member_dir.parent.name, member_dir.name, value))
        return out

    def audit(self) -> List[str]:
        """这场会哪里不对劲：副本对不上，或者有人写了不属于自己那条数据路径。

        写权不是靠约定：每个成员自己答了哪几步，它在会议记录里留了账（`member.run_bundle`），
        这里按"算子归哪个角色"核对——不是自己名下的，就报出来。查不出来的是伪造（那要靠副本
        对账和最后那个人签字）。
        """
        problems = [f"{path} + {name} 的副本对不上：{copies}"
                    for path, name, copies in self.reconcile()]
        for line in self.store.records():
            match = re.match(r"- (\S+)（(\w+)）答了 (\S+) \+ (\w+)$", line)
            if match is None:
                continue
            who, role, path, op = match.groups()
            owner = self.workflow.role_of_op(op)
            member = self.members.get(who)
            if owner is None:
                problems.append(f"{who} 答了谁也不认的算子 {op}（{path}）")
            elif owner != role or (member is not None and member.role != role):
                problems.append(f"{who}（{role}）答了 {op}，可那是 {owner} 名下的（{path}）")
        return problems

    def reconcile(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Where the copies disagree with the store — an empty list is a clean meeting."""
        problems: List[Tuple[str, str, Dict[str, Any]]] = []
        for data_path, name in self.store.stored():
            value, answered = self.store.read(data_path, name)
            if not answered:
                continue
            copies = {"store": value}
            for _ticket, member, copy in self.backups(data_path, name):
                copies[member] = copy
            if len({_text(copy) for copy in copies.values()}) > 1:
                problems.append((data_path, name, copies))
        return problems


def _text(value) -> str:
    """A stored value written as one line, for comparing copies."""
    piece = getattr(value, "data", None)
    if piece is None:
        return repr(value)
    return " ".join(viba_ast.unparse_type(piece).split())
