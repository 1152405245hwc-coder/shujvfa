"""主体归并：把「同一个人的不同称谓」识别出来，但绝不自动合并。

为什么这件事必须人工在环
------------------------
`normalize_party_name` 只去掉括号里的开户行信息，所以 ``李某`` / ``李某某`` / ``老李`` /
``李某甲`` 会被当成四个人，直接导致匹配失败、覆盖金额算少。这是真实存在的缺口。

但错误合并的后果比漏合并严重得多：把两个人的资金并成一个人的，会让不同被害人的钱
串成一案，属于**不可逆的事实性错误**。所以本模块的设计是：

1. 模型只**提出候选**，每条候选都必须带逐字原文依据，且依据必须能在材料中命中；
2. **确认粒度下沉到单个别名**。真实 API 实测中，模型会把「老李 = 李某」（证言直接支持）
   和「李某某 = 李某」（模型自述为间接推断）放进同一组并统一标为 high。
   若按组确认，弱的那条会被强的那条带着一起通过——所以每个别名各自持有依据、各自确认；
3. 候选一律标记为「待人工确认」，不进入任何计算；
4. 只有显式标注 ``status = 已确认`` 且写明确认人的别名，才能进入 ``PartyAliasRegistry``；
5. 之后所有名称比较仍走确定性代码，只是比较前先查一次映射表。

映射表可序列化并随报告留痕，因此「为什么把这两个名字并在一起」永远可回溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from legal_funds_agent.llm.base import LLMProvider
from legal_funds_agent.llm.schemas import (
    SCHEMA_PARTY_ALIAS,
    build_party_alias_input,
    supports_schema,
)

STATUS_CONFIRMED = "已确认"
STATUS_PENDING = "待人工确认"


@dataclass
class AliasProposal:
    provider: str
    checked: bool
    groups: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def pending_aliases(self) -> list[dict[str, Any]]:
        return [
            {"group_id": group["group_id"], "canonical_name": group["canonical_name"], **alias}
            for group in self.groups
            for alias in group["aliases"]
        ]


def propose_alias_groups(
    *,
    party_names: Iterable[str],
    materials: list[dict[str, str]],
    provider: LLMProvider | None,
) -> AliasProposal:
    """Ask the model which observed names may refer to the same person.

    ``materials`` is a list of ``{"label": ..., "text": ...}``. Every proposed alias must
    cite a span that literally occurs in one of those texts; an alias whose evidence cannot
    be located is dropped and recorded in ``notes`` rather than silently trusted.
    """
    names = sorted({str(name).strip() for name in party_names if str(name).strip()})
    if not names:
        return AliasProposal(provider="none", checked=False, notes=["NO_PARTY_NAMES"])
    if provider is None or not supports_schema(provider, SCHEMA_PARTY_ALIAS):
        return AliasProposal(
            provider=getattr(provider, "name", "none"),
            checked=False,
            notes=["PARTY_ALIAS_UNSUPPORTED_BY_PROVIDER"],
        )

    envelope = build_party_alias_input(names, materials)
    try:
        rows = provider.generate_structured(text=envelope, schema_name=SCHEMA_PARTY_ALIAS)
    except Exception as exc:  # noqa: BLE001 - a failed proposal must not break the case
        return AliasProposal(
            provider=getattr(provider, "name", "unknown"),
            checked=False,
            notes=[f"PARTY_ALIAS_CALL_FAILED:{type(exc).__name__}"],
        )

    corpus = "\n".join(str(material.get("text") or "") for material in materials)
    groups: list[dict[str, Any]] = []
    notes: list[str] = []

    for index, group in enumerate(rows, start=1):
        group_id = f"ALIAS-{index:03d}"
        verified: list[dict[str, Any]] = []
        for alias in group.get("aliases", []):
            evidence = []
            for item in alias.get("evidence", []):
                source_text = str(item.get("source_text") or "")
                if source_text and source_text in corpus:
                    evidence.append({"source_text": source_text, "reason": item.get("reason") or ""})
                elif source_text:
                    notes.append(f"{group_id}_{alias['name']}_EVIDENCE_NOT_FOUND:{source_text[:20]}")
            if not evidence:
                notes.append(f"{group_id}_{alias['name']}_REJECTED:no verifiable evidence")
                continue
            verified.append({
                "name": alias["name"],
                "confidence": alias.get("confidence", "low"),
                "evidence": evidence,
                "status": STATUS_PENDING,
            })
        if not verified:
            notes.append(f"{group_id}_REJECTED:no verifiable alias")
            continue
        groups.append({
            "group_id": group_id,
            "canonical_name": group["canonical_name"],
            "aliases": verified,
        })

    return AliasProposal(
        provider=getattr(provider, "name", "unknown"),
        checked=True,
        groups=groups,
        notes=notes,
    )


class PartyAliasRegistry:
    """An explicitly human-confirmed alias mapping.

    Construction refuses any alias that a human has not individually marked as confirmed,
    so a model proposal can never become a silent merge just by being passed along — and a
    well-attested alias cannot drag an inferred neighbour in with it.
    """

    def __init__(self, mapping: dict[str, str], *, confirmed_by: str, merges: list[dict[str, Any]]):
        self._mapping = dict(mapping)
        self.confirmed_by = confirmed_by
        self._merges = [dict(merge) for merge in merges]

    @classmethod
    def from_confirmed(cls, groups: list[dict[str, Any]], *, confirmed_by: str) -> "PartyAliasRegistry":
        if not confirmed_by or not str(confirmed_by).strip():
            raise ValueError("alias registry requires a named human confirmer")

        mapping: dict[str, str] = {}
        merges: list[dict[str, Any]] = []
        for group in groups:
            canonical = str(group.get("canonical_name") or "").strip()
            if not canonical:
                raise ValueError("alias group has no canonical_name")
            for alias in group.get("aliases") or []:
                name = str(alias.get("name") or "").strip()
                if not name or name == canonical:
                    continue
                if alias.get("status") != STATUS_CONFIRMED:
                    raise ValueError(
                        f"alias {name!r} is not human-confirmed; it is {alias.get('status')!r}"
                    )
                if name in mapping and mapping[name] != canonical:
                    raise ValueError(
                        f"conflicting merge: {name!r} is claimed by both "
                        f"{mapping[name]!r} and {canonical!r}"
                    )
                if mapping.get(canonical) == name:
                    raise ValueError(f"circular merge between {canonical!r} and {name!r}")
                mapping[name] = canonical
                merges.append({
                    "alias": name,
                    "canonical": canonical,
                    "group_id": group.get("group_id"),
                    "confidence": alias.get("confidence", "low"),
                    "evidence": list(alias.get("evidence") or []),
                })
        return cls(mapping, confirmed_by=str(confirmed_by).strip(), merges=merges)

    def resolve(self, name: str | None) -> str:
        """Map a name to its confirmed canonical form; unknown names pass through."""
        if not name:
            return ""
        text = str(name).strip()
        seen: set[str] = set()
        while text in self._mapping and text not in seen:
            seen.add(text)
            text = self._mapping[text]
        return text

    def merges(self) -> list[tuple[str, str]]:
        return sorted((merge["alias"], merge["canonical"]) for merge in self._merges)

    def __len__(self) -> int:
        return len(self._mapping)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed_by": self.confirmed_by,
            "merge_count": len(self._mapping),
            "merges": self._merges,
        }


def collect_party_names(claims: Iterable[Any], transactions: Iterable[Any]) -> list[str]:
    """Every distinct party name the case actually references."""
    names: set[str] = set()
    for claim in claims:
        for value in (claim.victim_name, claim.alleged_recipient_name):
            if value and str(value).strip():
                names.add(str(value).strip())
    for transaction in transactions:
        for value in (transaction.payer_name, transaction.payee_name):
            if value and str(value).strip():
                names.add(str(value).strip())
    return sorted(names)
