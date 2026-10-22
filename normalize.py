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
# CLI (partes seguintes acrescentam a extracao de skills e os arquivos de saida)
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normaliza o raw do scraper em data/")
    ap.add_argument("--pages", type=Path, default=Path("output/pages"))
    ap.add_argument("--data", type=Path, default=Path("data"))
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    warnings = Warnings()

    stats = load_stats(a.data / "stats.json", warnings)
    raws = load_raw_pages(a.pages, warnings)
    log.info("stats: %d ids, %d aliases; raws: %d", len(stats.by_id),
             len(stats.alias_to_id), len(raws))

    warnings.write(a.data / "reports" / "normalize_warnings.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
