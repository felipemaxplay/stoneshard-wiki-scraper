#!/usr/bin/env python3
"""
Normaliza os registros brutos do scraper (output/pages/*.json, schema 4) em
data/skills.json, usando data/stats.json como dicionario canonico.

Parte 1 (este arquivo): carregamento do dicionario de stats, indice de aliases e
coletor de avisos. A extracao das skills e feita nas proximas partes.

Regras:
  - Nunca falhar em caso desconhecido: o problema vira um aviso em
    data/reports/normalize_warnings.json (com id da skill e motivo).
  - Texto numerico e a fonte de exibicao; formulas sao complemento.
  - Formulas ficam como texto. Nada de AST no JSON. Avaliacao futura: modulo ast, sem eval().

Uso:
  python normalize.py                       # le output/pages, grava data/
  python normalize.py --pages output/pages --data data
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("normalize")


# --------------------------------------------------------------------------- #
# Avisos
# --------------------------------------------------------------------------- #

@dataclass
class Warnings:
    """Coleta avisos sem interromper o processamento."""
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, skill_id: str | None, reason: str, detail: Any = None) -> None:
        entry: dict[str, Any] = {"skill": skill_id, "reason": reason}
        if detail is not None:
            entry["detail"] = detail
        self.items.append(entry)
        log.warning("%s: %s", skill_id or "-", reason)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.items, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


# --------------------------------------------------------------------------- #
# Dicionario de stats (data/stats.json)
# --------------------------------------------------------------------------- #

def alias_key(text: str) -> str:
    """Chave de busca: minusculas, espacos e hifens/underlines unificados."""
    return re.sub(r"[\s\-_]+", " ", text.strip().lower())


@dataclass
class StatIndex:
    by_id: dict[str, dict[str, Any]]
    alias_to_id: dict[str, str]

    def resolve(self, text: str) -> str | None:
        """Nome, alias ou id -> id canonico. None se desconhecido."""
        return self.alias_to_id.get(alias_key(text))


def load_stats(path: Path, warnings: Warnings) -> StatIndex:
    data = json.loads(path.read_text(encoding="utf-8"))
    by_id: dict[str, dict[str, Any]] = {}
    alias_to_id: dict[str, str] = {}

    for stat in data.get("stats", []):
        sid = stat["id"]
        if sid in by_id:
            warnings.add(None, "id de stat duplicado em stats.json", sid)
            continue
        by_id[sid] = stat

    for sid, stat in by_id.items():
        names = [sid, stat.get("name", ""), *stat.get("aliases", [])]
        for name in names:
            if not name:
                continue
            key = alias_key(name)
            owner = alias_to_id.get(key)
            if owner and owner != sid:
                # Alias igual em dois stats: nao escolhemos sozinhos.
                warnings.add(None, "alias ambiguo em stats.json", {"alias": name,
                                                                   "ids": [owner, sid]})
                continue
            alias_to_id[key] = sid

    return StatIndex(by_id=by_id, alias_to_id=alias_to_id)


# --------------------------------------------------------------------------- #
# Entrada: registros brutos
# --------------------------------------------------------------------------- #

def load_raw_pages(pages_dir: Path, warnings: Warnings) -> list[dict[str, Any]]:
    """Le todos os raws. Arquivo corrompido vira aviso, nao erro."""
    records: list[dict[str, Any]] = []
    for path in sorted(pages_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.add(path.stem, "raw ilegivel", str(exc))
            continue
        if not isinstance(rec, dict) or "titulo" not in rec:
            warnings.add(path.stem, "raw sem campo titulo")
            continue
        rec.setdefault("_source_file", str(path))
        records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# Helpers gerais
# --------------------------------------------------------------------------- #

def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def first(value: Any) -> Any:
    items = as_list(value)
    return items[0] if items else None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


TREE_SUFFIX = re.compile(r"\s*\(skill tree\)$", re.I)


def tree_id(title: str) -> str:
    return slug(TREE_SUFFIX.sub("", title))


def attrs_of(data: dict[str, Any]) -> dict[str, Any]:
    """Aceita os dois formatos: achatado (pagina) e 'Basic Stats' aninhado (linha de arvore)."""
    basic = data.get("Basic Stats")
    flat = {k: v for k, v in data.items() if k != "Basic Stats"}
    if isinstance(basic, dict):
        return {**flat, **basic}
    if isinstance(basic, str):
        flat.setdefault("Classification", basic)
    return flat


# --------------------------------------------------------------------------- #
# Skills: constantes
# --------------------------------------------------------------------------- #

KNOWN_CLASSES = {"attack", "maneuver", "passive", "stance", "spell", "charge", "active"}
TARGET_TYPES = {"target object": "object", "target tile": "tile", "no target": "none",
                "target area": "area", "target point": "point"}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
TREATISE_RE = re.compile(r"Treatise\s+([IVX]+)\b")
SPLIT_RE = re.compile(r"(?=\bTo unlock this ability\b)|(?=\bUnlocked by reading\b)"
                      r"|(?=\bPassive bonus:)")
PASSIVE_RE = re.compile(r"^Passive bonus:?\s*(.*)$")
UNLOCK_LEVEL = re.compile(r"reach level (\d+)")
UNLOCK_POINTS = re.compile(r"(?:at least (\d+) points in the following attributes: |"
                           r"spending (\d+) Attribute points into )([^.]+)\.")
UNLOCK_TREATISE = re.compile(r"(?:Unlocked by reading (.+?) )?Treatise ([IVX]+)")
# Nome do stat logo apos o '%': so palavras com inicial maiuscula ('Counter Chance until...' -> 'Counter Chance')
PCT_STAT_RE = re.compile(r"%\s*((?:[A-Z][A-Za-z'\-]*)(?:\s+(?:&|[A-Z][A-Za-z'\-]*)){0,3}(?:\s+chance)?)")
VAR_RE = re.compile(r"[A-Z][A-Za-z]*(?:[ \-][A-Z][A-Za-z]*)*")


# --------------------------------------------------------------------------- #
# Skills: entrada (paginas individuais e linhas das arvores)
# --------------------------------------------------------------------------- #

def is_skill_page(rec: dict[str, Any]) -> bool:
    if rec.get("tipo_pagina") != "item":
        return False
    cls = first(attrs_of(rec.get("dados_estruturados", {})).get("Classification"))
    return isinstance(cls, str) and cls.split("/")[0].strip().lower() in KNOWN_CLASSES


def is_tree_page(rec: dict[str, Any]) -> bool:
    """Arvore de verdade: titulo '(skill tree)' ou categoria 'Skill trees'. Inimigos e NPCs listam skills, mas nao sao arvores."""
    return (rec["titulo"].lower().endswith("(skill tree)")
            or "Skill trees" in rec.get("categorias", []))


def collect_tree_rows(raws: list[dict[str, Any]], warnings: Warnings) -> dict[str, dict[str, Any]]:
    """Linhas 'Skill List' das paginas de arvore: skill -> arvore, treatise e atributos."""
    rows: dict[str, dict[str, Any]] = {}
    for rec in raws:
        if not is_tree_page(rec):
            continue
        for tab in rec.get("dados_estruturados", {}).get("tabelas", []):
            if tab.get("tipo") != "tabela" or "Skill" not in tab.get("colunas", []):
                continue
            for row in tab.get("linhas", []):
                name = first(row.get("Skill"))
                if not name:
                    continue
                key = slug(str(name))
                if key in rows:
                    warnings.add(key, "skill aparece em mais de uma arvore", rec["titulo"])
                    continue
                m = TREATISE_RE.search(str(row.get("_secao") or ""))
                desc = row.get("Description") or []
                rows[key] = {
                    "name": str(name),
                    "tree": tree_id(rec["titulo"]),
                    "treatise": ROMAN.get(m.group(1)) if m else None,
                    "attrs": attrs_of(row),
                    "description": desc if isinstance(desc, list) else [desc],
                    "source": rec["_source_file"],
                }
    return rows


def page_lines(rec: dict[str, Any]) -> list[str]:
    """Texto numerico da pagina (antes da secao '## Formulas')."""
    lines: list[str] = []
    for ln in (rec.get("conteudo_texto") or "").split("\n"):
        if ln.startswith("## "):
            break
        if ln.strip():
            lines.append(ln.strip())
    return lines


# --------------------------------------------------------------------------- #
# Skills: texto, unlock e formulas
# --------------------------------------------------------------------------- #

def split_text(lines: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Separa efeitos, bonus passivo da arvore e frases de unlock (inclusive coladas na mesma linha)."""
    effects: list[str] = []
    unlock: list[str] = []
    passive: str | None = None
    expect_passive = False
    for ln in lines:
        for part in (p.strip() for p in SPLIT_RE.split(ln)):
            if not part:
                continue
            if part.startswith("To unlock") or part.startswith("Unlocked by reading"):
                unlock.append(part)
            elif m := PASSIVE_RE.match(part):
                if m.group(1).strip():
                    passive = m.group(1).strip()
                else:
                    expect_passive = True
            elif expect_passive:
                passive, expect_passive = part, False
            else:
                effects.append(part)
    return effects, passive, unlock


def parse_unlock(text: str, skill_id: str, stats: StatIndex, warnings: Warnings) -> dict[str, Any] | None:
    if not text:
        return None
    level = UNLOCK_LEVEL.search(text)
    points = UNLOCK_POINTS.search(text)
    treat = UNLOCK_TREATISE.search(text)
    if not (level or points or treat):
        warnings.add(skill_id, "unlock fora do padrao", text)
        return None
    attrs: list[str] = []
    if points:
        for raw in re.split(r",\s*|\s+or\s+|\s+and\s+", points.group(3)):
            sid = stats.resolve(raw.strip())
            if sid:
                attrs.append(sid)
            else:
                warnings.add(skill_id, "atributo de unlock desconhecido", raw.strip())
    return {
        "treatise": ({"tree": slug(treat.group(1)) if treat.group(1) else None,
                      "number": ROMAN.get(treat.group(2))} if treat else None),
        "level": int(level.group(1)) if level else None,
        "attribute_points": ({"min": int(points.group(1) or points.group(2)),
                              "attributes": attrs} if points else None),
    }


def match_open(text: str, close: int) -> int:
    """Indice do '(' que fecha o ')' em `close`; -1 se nao houver."""
    depth = 0
    for i in range(close, -1, -1):
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return -1


def formula_expr(text: str, pct: int) -> str | None:
    """Expressao entre parenteses antes do '%' (inclui grupos somados: '(a) + (b)%')."""
    j = pct - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0 or text[j] != ")":
        return None
    start = None
    while True:
        open_ = match_open(text, j)
        if open_ < 0:
            return None
        start = open_
        k = open_ - 1
        while k >= 0 and text[k].isspace():
            k -= 1
        if k >= 0 and text[k] in "+-*/":
            m = k - 1
            while m >= 0 and text[m].isspace():
                m -= 1
            if m >= 0 and text[m] == ")":
                j = m
                continue
        break
    return text[start:pct].strip()


def parse_formulas(raw_lines: list[str], skill_id: str, stats: StatIndex,
                   warnings: Warnings) -> list[dict[str, Any]]:
    """'+(AGL + PRC)% Weapon Damage' -> {raw, expr, vars, stat}. Texto sem stat fica so no texto."""
    out: list[dict[str, Any]] = []
    for raw in raw_lines:
        for m in PCT_STAT_RE.finditer(raw):
            expr = formula_expr(raw, m.start())
            if not expr:
                continue
            name = m.group(1).strip()
            stat = stats.resolve(name)
            if stat is None:
                warnings.add(skill_id, "stat de formula desconhecido", {"stat": name, "raw": raw})
                continue
            vars_: list[str] = []
            for v in VAR_RE.findall(expr):
                vid = stats.resolve(v)
                if vid:
                    vars_.append(vid)
                else:
                    warnings.add(skill_id, "variavel de formula desconhecida", {"var": v, "raw": raw})
            out.append({"raw": raw, "expr": expr, "vars": sorted(set(vars_)), "stat": stat})
    return out


def assign_formulas(effects: list[str], formulas: list[dict[str, Any]], stats: StatIndex):
    """Liga cada formula ao efeito que cita o mesmo stat; sem efeito correspondente, fica na skill."""
    result = [{"text": t, "formulas": []} for t in effects]
    loose: list[dict[str, Any]] = []
    for f in formulas:
        label = stats.by_id[f["stat"]]["name"].lower()
        hit = next((e for e in result if label in e["text"].lower()), None)
        (hit["formulas"] if hit else loose).append(f)
    return result, loose


# --------------------------------------------------------------------------- #
# Skills: montagem
# --------------------------------------------------------------------------- #

def build_skill(key: str, page: dict[str, Any] | None, row: dict[str, Any] | None,
                stats: StatIndex, warnings: Warnings) -> dict[str, Any]:
    attrs = {**(row["attrs"] if row else {}),
             **(attrs_of(page.get("dados_estruturados", {})) if page else {})}
    name = page["titulo"] if page else row["name"]

    lines = page_lines(page) if page else row["description"]
    effects_text, passive, unlock_parts = split_text([str(x) for x in lines])
    unlock = parse_unlock(" ".join(unlock_parts), key, stats, warnings)
    unlock_tree = (unlock or {}).get("treatise") and unlock["treatise"]["tree"]
    if row and unlock_tree and unlock_tree != row["tree"]:
        warnings.add(key, "arvore da linha difere da arvore do unlock",
                     {"linha": row["tree"], "unlock": unlock_tree})

    formula_lines = [str(x) for x in as_list(attrs.get("Formulas"))]
    formulas = parse_formulas(formula_lines, key, stats, warnings)
    effects, loose = assign_formulas(effects_text, formulas, stats)

    tree = row["tree"] if row else None
    if tree is None and unlock_tree:
        tree = unlock_tree
    if tree is None and page:
        crumb = next((c for c in page.get("breadcrumbs", [])
                      if c.lower().endswith("(skill tree)")), None)
        tree = tree_id(crumb) if crumb else None
    if tree is None:
        warnings.add(key, "arvore nao encontrada")

    treatise = row["treatise"] if row and row["treatise"] else None
    if treatise is None and unlock and unlock["treatise"]:
        treatise = unlock["treatise"]["number"]
    if treatise is None:
        warnings.add(key, "treatise nao encontrado")

    classes: list[str] = []
    for part in str(first(attrs.get("Classification")) or "").split("/"):
        token = part.strip().lower()
        if token in KNOWN_CLASSES:
            classes.append(token)
        elif token:
            warnings.add(key, "classificacao desconhecida", token)

    cost = {"energy": first(attrs.get("Energy")), "cooldown": first(attrs.get("Cooldown"))}

    target = None
    ttype_raw = first(attrs.get("Type"))
    if ttype_raw is not None or attrs.get("Range") is not None:
        ttype = TARGET_TYPES.get(str(ttype_raw or "").strip().lower())
        if ttype is None and ttype_raw is not None:
            warnings.add(key, "tipo de alvo desconhecido", ttype_raw)
        target = {"type": ttype, "range": first(attrs.get("Range"))}

    requirements: list[dict[str, str]] = []
    for req in as_list(attrs.get("Requirement")):
        text = str(req).strip()
        if re.fullmatch(r"Requires dual weapons", text):
            requirements.append({"type": "weapon_setup", "value": "dual"})
        elif m := re.fullmatch(r"Requires (?:an? )?(.+)", text):
            requirements.append({"type": "weapon", "value": m.group(1).strip().lower()})
        else:
            requirements.append({"type": "text", "value": text})
            warnings.add(key, "requisito fora do padrao", text)

    scales: list[str] = []
    for item in as_list(attrs.get("Modified by")):
        for part in str(item).split(","):
            if not part.strip():
                continue
            sid = stats.resolve(part)
            if sid:
                scales.append(sid)
            else:
                warnings.add(key, "stat em Modified by desconhecido", part.strip())

    return {
        "id": key,
        "name": str(name),
        "tree": tree,
        "treatise": treatise,
        "classification": classes,
        "cost": cost,
        "target": target,
        "scales_with": scales,
        "requirements": requirements,
        "unlock": unlock,
        "passive_bonus": passive,
        "effects": effects,
        "formulas": loose,
        "source": {"page": page["_source_file"] if page else None,
                   "tree": row["source"] if row else None},
    }


def build_all(raws: list[dict[str, Any]], stats: StatIndex,
              warnings: Warnings) -> tuple[list[dict[str, Any]], list[str]]:
    rows = collect_tree_rows(raws, warnings)
    pages: dict[str, dict[str, Any]] = {}
    for rec in raws:
        if not is_skill_page(rec):
            continue
        key = slug(rec["titulo"])
        if key in pages:
            warnings.add(key, "pagina de skill duplicada", rec["_source_file"])
            continue
        pages[key] = rec
    skills: list[dict[str, Any]] = []
    outside: list[str] = []
    for key in sorted(set(pages) | set(rows)):
        local = Warnings()
        skill = build_skill(key, pages.get(key), rows.get(key), stats, local)
        # Habilidade de inimigo (pagina sem arvore nem treatise): fica fora de skills.json.
        if key not in rows and not skill["tree"] and skill["treatise"] is None:
            outside.append(key)
            continue
        warnings.items.extend(local.items)
        skills.append(skill)
    skills.sort(key=lambda s: (s["tree"] or "", s["treatise"] or 0, s["name"]))
    return skills, outside


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Arvores (trees.json)
# --------------------------------------------------------------------------- #

PENALTY_RE = re.compile(r"([A-Z][A-Za-z\- ]*?)\s+([+\-−]\d+(?:\.\d+)?%?)")


def passive_text(value: Any) -> str | None:
    """Linha de arvore: 'Passive bonus' vem como texto ou como {'Passive bonus': texto}."""
    if isinstance(value, dict):
        value = value.get("Passive bonus")
    value = first(value)
    return str(value).strip() if value else None


def build_trees(raws: list[dict[str, Any]], skills: list[dict[str, Any]],
                stats: StatIndex, warnings: Warnings) -> list[dict[str, Any]]:
    skills_by_tree: dict[str, list[dict[str, Any]]] = {}
    for s in skills:
        skills_by_tree.setdefault(s["tree"], []).append(s)

    trees: list[dict[str, Any]] = []
    for rec in raws:
        if not is_tree_page(rec):
            continue
        tables = [t for t in rec.get("dados_estruturados", {}).get("tabelas", [])
                  if t.get("tipo") == "tabela" and "Skill" in t.get("colunas", [])]
        if not tables:
            continue
        tid = tree_id(rec["titulo"])
        lines = page_lines(rec)

        main_focus = None
        penalty_text = None
        for i, ln in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            if ln.lower() == "main focus:" and nxt:
                main_focus = nxt
            if ln.lower().endswith("penalty:") and nxt:
                penalty_text = nxt

        penalty = None
        if penalty_text:
            mods = []
            for name, value in PENALTY_RE.findall(penalty_text):
                sid = stats.resolve(name.strip())
                num = float(value.replace("−", "-").rstrip("%"))
                if sid:
                    mods.append({"stat": sid, "value": num})
                else:
                    warnings.add(tid, "stat de penalidade de arvore desconhecido", name.strip())
            penalty = {"text": penalty_text, "modifiers": mods}

        passives = {p for t in tables for row in t.get("linhas", [])
                    if (p := passive_text(row.get("Passive bonus")))}
        passive: Any = None
        if len(passives) == 1:
            passive = passives.pop()
        elif len(passives) > 1:
            passive = sorted(passives)
            warnings.add(tid, "passive bonus diferente entre linhas da arvore", passive)

        members = sorted(skills_by_tree.get(tid, []),
                         key=lambda s: (s["treatise"] or 0, s["name"]))
        trees.append({
            "id": tid,
            "name": TREE_SUFFIX.sub("", rec["titulo"]),
            "main_focus": main_focus,
            "penalty": penalty,
            "passive_bonus": passive,
            "skills": [s["id"] for s in members],
            "source": rec["_source_file"],
        })
    trees.sort(key=lambda t: t["id"])
    return trees


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normaliza o raw do scraper em data/")
    ap.add_argument("--pages", type=Path, default=Path("output/pages"))
    ap.add_argument("--data", type=Path, default=Path("data"))
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    warnings = Warnings()

    if not a.pages.is_dir():
        warnings.add(None, "pasta de raws nao encontrada", str(a.pages))

    stats = load_stats(a.data / "stats.json", warnings)
    raws = load_raw_pages(a.pages, warnings)
    skills, outside = build_all(raws, stats, warnings)

    trees = build_trees(raws, skills, stats, warnings)

    out = a.data / "skills.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(skills, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (a.data / "trees.json").write_text(json.dumps(trees, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    warnings.write(a.data / "reports" / "normalize_warnings.json")
    (a.data / "reports" / "non_tree_skills.json").write_text(
        json.dumps(sorted(outside), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    log.info("raws: %d; skills: %d; fora de arvore: %d; avisos: %d -> %s", len(raws),
             len(skills), len(outside), len(warnings.items), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
