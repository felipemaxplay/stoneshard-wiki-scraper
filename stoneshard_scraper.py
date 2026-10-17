#!/usr/bin/env python3
"""
Stoneshard Wiki -> base de dados local em JSON (+ imagens).

Fluxo:
  1. Detecta o api.php da wiki (MediaWiki 1.45.x em stoneshard.com/wiki).
  2. Semeia a fila BFS com os links da sidebar (MediaWiki:Sidebar -> "Gear", "Consumables"...)
     e com os links da pagina inicial. O titulo da secao vira o 1o item do breadcrumb.
  3. Para cada pagina: action=parse -> HTML renderizado + wikitext + categorias.
       - infobox  -> dict chave/valor (heuristicas genericas, ver parse_infobox)
       - wikitable -> listas de registros em dados_estruturados["tabelas"]
       - texto limpo, links filhos, imagens (resolucao original)
  4. Paginas com infobox (itens) sao "folhas": nao expandimos seus links.
  5. Grava output/pages/<titulo>.json (permite retomar) e output/stoneshard_db.json.

Uso:
  python stoneshard_scraper.py --max-pages 30            # teste curto
  python stoneshard_scraper.py --section Gear            # so a secao Gear
  python stoneshard_scraper.py                           # tudo
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import mwparserfromhell
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("stoneshard")

# --------------------------------------------------------------------------- #
# Configuracao e constantes
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    site_root: str = "https://stoneshard.com"
    wiki_path: str = "/wiki"
    main_page: str = "Stoneshard_Wiki"
    out_dir: Path = Path("output")
    delay: float = 1.0               # pausa minima entre requisicoes (s)
    max_depth: int = 6               # tamanho maximo do breadcrumb
    max_pages: int = 0               # 0 = sem limite
    only_sections: list[str] = field(default_factory=list)
    expand_leaves: bool = False      # True = tambem segue links de paginas de item
    download_images: bool = True
    refresh: bool = False            # ignora cache de paginas ja salvas
    home_label: str = "Stoneshard Wiki"
    user_agent: str = ("StoneshardOfflineArchiver/1.0 "
                       "(arquivo pessoal offline; contato: voce@exemplo.com)")
    ignore_images: set[str] = field(default_factory=lambda: {
        "Note1.png", "Stoneshard.png", "Mediawiki_compact.svg"})

    @property
    def base_url(self) -> str:
        return f"{self.site_root}{self.wiki_path}/"

    @property
    def api_candidates(self) -> list[str]:
        return [f"{self.site_root}{self.wiki_path}/api.php",
                f"{self.site_root}/w/api.php",
                f"{self.site_root}/api.php"]


IGNORED_NAMESPACES = {"file", "category", "special", "talk", "template", "template talk",
                      "user", "user talk", "mediawiki", "mediawiki talk", "help", "module",
                      "project", "stoneshard wiki", "media", "file talk", "category talk"}
LANG_SUBPAGE = re.compile(r"/(cz|zh|es|pt-br|ru|uk|de|fr|pl|ja|ko|tr|it)$", re.I)
NON_PAGE_PREFIXES = ("images/", "resources/", "extensions/", "skins/", "api.php",
                     "index.php", "load.php")
NOISE_SELECTORS = [".navbox", ".toc", "#toc", ".mw-editsection", ".ambox", ".mbox-small",
                   ".mw-message-box", ".metadata", ".noprint", ".catlinks", "style",
                   "script", "sup.reference"]
NAV_TABLE_TITLES = {"items", "languages"}      # tabelas de navegacao sem classe CSS
SEE_ALSO_TITLES = {"see also", "references", "external links"}
SIDEBAR_SKIP = {"navigation", "search", "toolbox", "languages"}

META_RE = re.compile(r"(Page last edited during patch|Data Page last updated in):\s*([\w.\-]+)")
TIER_RE = re.compile(r"^Tier\s+(\d+)$", re.I)
NUM_LABEL_RE = re.compile(r"^([+\-\u2212]?\d+(?:[.,]\d+)?%?)\s+([A-Za-z][A-Za-z' ]+)$")
PAIR_RE = re.compile(r"([A-Za-z][A-Za-z '\u2019/&-]*?):\s*([+\-\u2212]?\d[\d.,/]*%?)")
THUMB_RE = re.compile(r"/images/thumb/([0-9a-f]/[0-9a-f]{2})/([^/]+)/[^/]+$")


# --------------------------------------------------------------------------- #
# Utilitarios de texto/titulo
# --------------------------------------------------------------------------- #

def norm(title: str) -> str:
    """Normaliza titulo MediaWiki: '_' -> ' ' e 1a letra maiuscula."""
    t = title.replace("_", " ").strip()
    return t[:1].upper() + t[1:]


def is_content_title(title: str) -> bool:
    if not title or title.startswith(NON_PAGE_PREFIXES) or LANG_SUBPAGE.search(title):
        return False
    if ":" in title and title.split(":", 1)[0].strip().lower() in IGNORED_NAMESPACES:
        return False
    return True


def clean(el: Any, sep: str = "") -> str:
    txt = el.get_text(sep) if hasattr(el, "get_text") else str(el)
    return re.sub(r"\s+", " ", txt.replace("\xa0", " ")).strip()


def convert(val: str) -> Any:
    """'80' -> 80, '1.5' -> 1.5, '+5%' / '80/80' continuam string."""
    v = val.strip().replace("\u2212", "-")
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"[+-]?\d+\.\d+", v):
        return float(v)
    return val.strip()


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


def put(attrs: dict, key: str, value: Any) -> None:
    """Insere chave; se repetida, vira lista (nao perde dados)."""
    key = key.strip().rstrip(":").strip()
    if not key or value in ("", None):
        return
    if key not in attrs:
        attrs[key] = value
        return
    cur = attrs[key]
    if not isinstance(cur, list):
        cur = [cur]
    if value not in cur:
        cur.append(value)
    attrs[key] = cur


def href_to_title(href: str, cfg: Config) -> str | None:
    """Converte href interno da wiki em titulo de pagina (ou None)."""
    p = urlparse(urljoin(cfg.base_url, href))
    if p.netloc and p.netloc != urlparse(cfg.site_root).netloc:
        return None
    title = None
    if p.path.startswith(cfg.wiki_path + "/"):
        title = unquote(p.path[len(cfg.wiki_path) + 1:])
    elif p.path.endswith("index.php"):
        q = parse_qs(p.query)
        if "redlink" in q or q.get("action", ["view"]) != ["view"]:
            return None
        title = (q.get("title") or [None])[0]
    if not title:
        return None
    title = norm(title)
    return title if is_content_title(title) else None


# --------------------------------------------------------------------------- #
# Cliente da API (rede, retries, delays)
# --------------------------------------------------------------------------- #

class WikiApiError(Exception):
    def __init__(self, code: str, info: str = ""):
        super().__init__(f"{code}: {info}")
        self.code = code


class WikiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.user_agent
        retry = Retry(total=5, backoff_factor=1.5, respect_retry_after_header=True,
                      status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
        for scheme in ("https://", "http://"):
            self.session.mount(scheme, HTTPAdapter(max_retries=retry))
        self._last = 0.0
        self.api_url = self._detect_api()

    def _throttle(self, base: float | None = None) -> None:
        base = self.cfg.delay if base is None else base
        wait = base + random.uniform(0, base * 0.3) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _detect_api(self) -> str:
        for url in self.cfg.api_candidates:
            try:
                r = self.session.get(url, params={"action": "query", "meta": "siteinfo",
                                                  "format": "json"}, timeout=20)
                if r.ok and "query" in r.json():
                    log.info("API encontrada: %s", url)
                    return url
            except (requests.RequestException, ValueError) as exc:
                log.debug("api candidata falhou %s: %s", url, exc)
        raise RuntimeError("api.php nao encontrado; ajuste --site-root / --wiki-path")

    def api(self, **params: Any) -> dict:
        params.update(format="json", formatversion=2, maxlag=5)
        last_exc: Exception | None = None
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.get(self.api_url, params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("falha de rede/JSON (tentativa %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue
            err = data.get("error")
            if err:
                if err.get("code") in ("maxlag", "ratelimited"):
                    time.sleep(5 * (attempt + 1))
                    continue
                raise WikiApiError(err.get("code", "?"), err.get("info", ""))
            return data
        raise WikiApiError("network", str(last_exc))

    def parse_page(self, title: str) -> dict:
        data = self.api(action="parse", page=title, redirects=1,
                        prop="text|wikitext|categories|displaytitle",
                        disableeditsection=1, disabletoc=1)
        return data["parse"]

    def sidebar_sections(self) -> dict[str, list[str]]:
        """Le MediaWiki:Sidebar -> {'Gear': ['Weapons', 'Armor', ...], ...}."""
        try:
            data = self.api(action="query", meta="allmessages", ammessages="sidebar")
            msg = data["query"]["allmessages"][0].get("content", "")
        except (WikiApiError, KeyError, IndexError) as exc:
            log.warning("nao consegui ler a sidebar: %s", exc)
            return {}
        sections: dict[str, list[str]] = {}
        current = None
        for raw in msg.splitlines():
            line = raw.strip()
            if line.startswith("**") and current:
                target = line[2:].split("|")[0].strip()
                if target and "-url" not in target and "mainpage" not in target \
                        and not target.startswith("http"):
                    t = norm(target)
                    if is_content_title(t):
                        sections[current].append(t)
            elif line.startswith("*"):
                head = line[1:].strip()
                current = None if head.lower() in SIDEBAR_SKIP else head
                if current:
                    sections.setdefault(current, [])
        return {k: v for k, v in sections.items() if v}

    def download(self, url: str) -> bytes | None:
        self._throttle(self.cfg.delay * 0.5)
        try:
            r = self.session.get(url, timeout=30)
        except requests.RequestException as exc:
            log.warning("download falhou %s: %s", url, exc)
            return None
        if r.status_code != 200 or "text/html" in r.headers.get("Content-Type", ""):
            log.warning("download invalido (%s) %s", r.status_code, url)
            return None
        return r.content


# --------------------------------------------------------------------------- #
# Parsing de HTML
# --------------------------------------------------------------------------- #

def make_soup(html: str, drop_see_also: bool = False) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for sel in NOISE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()
    for table in soup.find_all("table"):
        if table.decomposed:
            continue
        first = next(iter(table.stripped_strings), "").lower()
        if first in NAV_TABLE_TITLES:
            table.decompose()
    if drop_see_also:
        _drop_sections(soup, SEE_ALSO_TITLES)
    return soup


def _drop_sections(soup: BeautifulSoup, titles: set[str]) -> None:
    heading = re.compile(r"^h[1-6]$")
    for h in soup.find_all(heading):
        if h.decomposed or clean(h).lower() not in titles:
            continue
        level = int(h.name[1])
        wrapper = h.parent if h.parent and "mw-heading" in (h.parent.get("class") or []) else h
        doomed = []
        for sib in wrapper.find_next_siblings():
            hh = sib if heading.match(sib.name or "") else (
                sib.find(heading) if "mw-heading" in (sib.get("class") or []) else None)
            if hh is not None and int(hh.name[1]) <= level:
                break
            doomed.append(sib)
        for s in doomed:
            s.decompose()
        wrapper.decompose()


def extract_links(soup: BeautifulSoup, cfg: Config) -> list[str]:
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        if "new" in (a.get("class") or []):          # redlink
            continue
        t = href_to_title(a["href"], cfg)
        if t and t not in out:
            out.append(t)
    return out


def to_original_url(url: str) -> str:
    """/images/thumb/a/ab/File.png/120px-File.png -> /images/a/ab/File.png"""
    return THUMB_RE.sub(r"/images/\1/\2", url)


def collect_images(root: Any, cfg: Config) -> list[tuple[str, str]]:
    """[(url_original, url_fallback)] sem duplicatas, na ordem do DOM."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for img in root.find_all("img"):
        src = img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        full = urljoin(cfg.site_root + "/", src)
        orig = to_original_url(full)
        name = unquote(urlparse(orig).path.rsplit("/", 1)[-1])
        if name in cfg.ignore_images or orig in seen:
            continue
        seen.add(orig)
        found.append((orig, full))
    return found


def find_infobox(root: BeautifulSoup) -> Any | None:
    for sel in ("table.infobox", ".infobox", ".portable-infobox", "table[class*=infobox]"):
        el = root.select_one(sel)
        if el:
            return el
    # Heuristica: 1a tabela "solta" (nao wikitable) com imagem e pouco link
    for t in root.find_all("table"):
        if "wikitable" in (t.get("class") or []) or t.find_parent("table"):
            continue
        strings = list(t.stripped_strings)
        if t.find("img") and 4 <= len(strings) <= 60 and len(t.find_all("a")) <= 8:
            return t
    return None


def parse_infobox(box: Any, title: str) -> dict[str, Any]:
    """
    Extrai pares chave/valor de uma infobox de formato desconhecido:
      a) linhas <tr> com 2 celulas curtas            -> chave/valor direto
      b) 'Chave:' em um no e valor no proximo         -> par
      c) 'Chave: valor' na mesma linha                -> par
      d) '17 Slashing Damage' -> Damage: 'Slashing: 17'; 'Tier 2' -> Tier: 2
      e) icones com alt (ex.: Price) viram 'Price: 475'
      f) sobras: linha longa -> Description; 1a curta -> Classification; resto -> Extras
    """
    attrs: dict[str, Any] = {}
    for tr in box.find_all("tr"):
        if tr.decomposed or tr.find("table"):
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2:
            k, v = clean(cells[0], " "), clean(cells[1], " ")
            if k and v and len(k) <= 40 and not cells[0].find("img"):
                put(attrs, k, convert(v))
                tr.decompose()

    for img in box.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(f"\n{alt}: " if alt and alt.lower() != title.lower() else "\n")

    lines = [re.sub(r"\s+", " ", ln.replace("\xa0", " ")).strip()
             for ln in box.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln and set(ln) != {"-"}]
    leftovers: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if len(ln) > 80:
            leftovers.append(ln)
            continue
        if ln.endswith(":") and i < len(lines) and not lines[i].endswith(":"):
            put(attrs, ln[:-1], convert(lines[i]))
            i += 1
            continue
        if ln.count(":") >= 2 and (pairs := PAIR_RE.findall(ln)):
            for k, v in pairs:
                put(attrs, k, convert(v))
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            if v.strip() and len(k.split()) <= 4 and "." not in k:
                put(attrs, k, convert(v))
                continue
        if m := TIER_RE.match(ln):
            put(attrs, "Tier", int(m.group(1)))
            continue
        if m := NUM_LABEL_RE.match(ln):
            num, label = m.group(1), m.group(2).strip()
            if label.lower().endswith(" damage"):
                dmg = f"{label[:-7].strip()}: {num}"
                attrs["Damage"] = f"{attrs['Damage']}, {dmg}" if "Damage" in attrs else dmg
            else:
                put(attrs, label, convert(num))
            continue
        leftovers.append(ln)

    leftovers = [ln for ln in leftovers if ln.lower() != title.lower()]
    desc = [ln for ln in leftovers if len(ln) > 40]
    short = [ln for ln in leftovers if len(ln) <= 40]
    if desc:
        put(attrs, "Description", max(desc, key=len))
    if short:
        put(attrs, "Classification", short[0])
        if len(short) > 1:
            attrs["Extras"] = short[1:]
    return attrs


def parse_wikitables(root: Any) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    for idx, t in enumerate(root.select("table.wikitable"), 1):
        rows = t.find_all("tr")
        if not rows:
            continue
        first = rows[0].find_all(["th", "td"])
        if first and all(c.name == "th" for c in first):
            header, body = [clean(c, " ") for c in first], rows[1:]
        else:
            header, body = [f"col{n}" for n in range(1, len(first) + 1)], rows
        seen: dict[str, int] = {}
        uniq = []
        for h in header:                       # garante chaves unicas
            h = h or "col"
            seen[h] = seen.get(h, 0) + 1
            uniq.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
        records = []
        for tr in body:
            vals = [clean(c, " ") for c in tr.find_all(["th", "td"])]
            if not any(vals):
                continue
            records.append(dict(zip(uniq, vals)) if len(vals) == len(uniq) else {"_row": vals})
        cap = t.find("caption")
        name = clean(cap) if cap and clean(cap) else f"tabela_{idx}"
        tables[name] = records
    return tables


def _own_text(el: Any) -> str:
    """Texto de um <li>/<p> sem o de listas aninhadas."""
    parts = []
    for c in el.children:
        if getattr(c, "name", None) in ("ul", "ol", "dl"):
            continue
        parts.append(c.get_text() if hasattr(c, "get_text") else str(c))
    return re.sub(r"\s+", " ", "".join(parts).replace("\xa0", " ")).strip()


def extract_text(root: Any) -> tuple[str, dict[str, str]]:
    meta: dict[str, str] = {}
    lines: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dd", "blockquote"]):
        txt = _own_text(el)
        if not txt:
            continue
        for k, v in META_RE.findall(txt):
            meta[k] = v
        txt = META_RE.sub("", txt).strip(" |")
        if not txt:
            continue
        if el.name[0] == "h" and el.name[1:].isdigit():
            lines.append(f"\n{'#' * int(el.name[1])} {txt}")
        elif el.name == "li":
            lines.append(f"- {txt}")
        else:
            lines.append(txt)
    return "\n".join(lines).strip(), meta


def wikitext_templates(wikitext: str) -> dict[str, dict[str, Any]]:
    """Fallback: parametros de templates de topo (mwparserfromhell)."""
    out: dict[str, dict[str, Any]] = {}
    for tpl in mwparserfromhell.parse(wikitext).filter_templates(recursive=False):
        params = {}
        for p in tpl.params:
            val = mwparserfromhell.parse(str(p.value)).strip_code().strip()
            if val:
                params[str(p.name).strip()] = convert(val)
        if params:
            out[str(tpl.name).strip()] = params
    return out


# --------------------------------------------------------------------------- #
# Imagens
# --------------------------------------------------------------------------- #

class ImageStore:
    def __init__(self, cfg: Config, client: WikiClient):
        self.client = client
        self.dir = cfg.out_dir / "assets" / "img"
        self.dir.mkdir(parents=True, exist_ok=True)

    def fetch(self, original: str, fallback: str) -> str | None:
        """Baixa a imagem e devolve o caminho LOCAL relativo (nunca a URL)."""
        name = safe_filename(unquote(urlparse(original).path.rsplit("/", 1)[-1]))
        dest = self.dir / name
        rel = f"assets/img/{name}"
        if dest.exists() and dest.stat().st_size > 0:
            return rel
        for url in dict.fromkeys((original, fallback)):
            data = self.client.download(url)
            if data:
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(data)
                tmp.replace(dest)
                return rel
        return None


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #

class Crawler:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        (cfg.out_dir / "pages").mkdir(exist_ok=True)
        self.client = WikiClient(cfg)
        self.images = ImageStore(cfg, self.client) if cfg.download_images else None
        self.visited: set[str] = set()
        self.queue: deque[tuple[str, list[str]]] = deque()
        self.queued: set[str] = set()
        self.records: list[dict] = []

    # -- fila -----------------------------------------------------------------
    def enqueue(self, title: str, parent_crumbs: list[str]) -> None:
        key = norm(title)
        if key not in self.queued and key not in self.visited:
            self.queued.add(key)
            self.queue.append((key, parent_crumbs))

    def seed(self) -> None:
        cfg = self.cfg
        only = {s.lower() for s in cfg.only_sections}
        self.visited.add(norm(cfg.main_page))
        for section, targets in self.client.sidebar_sections().items():
            if only and section.lower() not in only:
                continue
            for t in targets:
                self.enqueue(t, [section])
        if not only:
            main = self.client.parse_page(cfg.main_page)
            for t in extract_links(make_soup(main["text"]), cfg):
                self.enqueue(t, [cfg.home_label])
        log.info("fila inicial: %d paginas", len(self.queue))

    # -- cache ----------------------------------------------------------------
    def _page_file(self, title: str) -> Path:
        slug = re.sub(r"[^\w\-.'() ]+", "_", title).strip().replace(" ", "_")[:150]
        return self.cfg.out_dir / "pages" / f"{slug}.json"

    def _read_cache(self, title: str) -> dict | None:
        f = self._page_file(title)
        if self.cfg.refresh or not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- processamento de uma pagina -------------------------------------------
    def build_record(self, parsed: dict, parent_crumbs: list[str]) -> dict:
        cfg = self.cfg
        title = norm(parsed["title"])
        html = parsed["text"]
        soup = make_soup(html)
        children = extract_links(make_soup(html, drop_see_also=True), cfg)

        box = find_infobox(soup)
        refs = collect_images(box, cfg) if box else []
        refs += [r for r in collect_images(soup, cfg) if r not in refs]

        attrs: dict[str, Any] = {}
        if box is not None:
            attrs = parse_infobox(box, title)
            box.decompose()
        is_item = len(attrs) >= 2

        tables = parse_wikitables(soup)
        for t in soup.find_all("table"):
            if not t.decomposed:
                t.decompose()
        text, meta = extract_text(soup)
        for k, v in meta.items():
            put(attrs, k, v)
        if tables:
            attrs["tabelas"] = tables
        if not is_item:
            tpls = wikitext_templates(parsed.get("wikitext", ""))
            if tpls:
                attrs["_wikitext_templates"] = tpls
        if not text and parsed.get("wikitext"):
            text = mwparserfromhell.parse(parsed["wikitext"]).strip_code().strip()

        images = []
        if self.images:
            for orig, fb in refs:
                path = self.images.fetch(orig, fb)
                if path and path not in images:
                    images.append(path)

        crumbs = parent_crumbs + [title]
        if is_item and not cfg.expand_leaves:
            children = []
        return {
            "breadcrumbs": crumbs,
            "titulo": title,
            "conteudo_texto": text,
            "dados_estruturados": attrs,
            "imagens": images,
            "tipo_pagina": "item" if is_item else "hub",
            "categorias": [c["category"].replace("_", " ") for c in parsed.get("categories", [])
                           if not c.get("hidden")],
            "filhos": children,
        }

    def process(self, title: str, parent_crumbs: list[str]) -> dict | None:
        cached = self._read_cache(title)
        if cached:
            return cached
        parsed = self.client.parse_page(title)
        real = norm(parsed["title"])
        if real != title:                       # redirect
            if real in self.visited:
                log.info("redirect %s -> %s ja visitado", title, real)
                return None
            self.visited.add(real)
        record = self.build_record(parsed, parent_crumbs)
        f = self._page_file(real)
        f.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    # -- loop principal -----------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        self.seed()
        n = 0
        while self.queue and (cfg.max_pages == 0 or n < cfg.max_pages):
            title, parent_crumbs = self.queue.popleft()
            if title in self.visited:
                continue
            self.visited.add(title)
            try:
                rec = self.process(title, parent_crumbs)
            except WikiApiError as exc:
                log.warning("pulando %s (%s)", title, exc)
                continue
            except Exception:                    # noqa: BLE001 - nunca derrubar o crawl
                log.exception("erro inesperado em %s", title)
                continue
            if rec is None:
                continue
            n += 1
            self.records.append(rec)
            log.info("[%d] %s (%s) fila=%d", n, " > ".join(rec["breadcrumbs"]),
                     rec["tipo_pagina"], len(self.queue))
            if len(rec["breadcrumbs"]) < cfg.max_depth:
                for child in rec["filhos"]:
                    self.enqueue(child, rec["breadcrumbs"])
        self.finish()

    def finish(self) -> None:
        self.records.sort(key=lambda r: r["breadcrumbs"])
        out = self.cfg.out_dir / "stoneshard_db.json"
        out.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("concluido: %d paginas -> %s", len(self.records), out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scraper offline da Stoneshard Wiki")
    ap.add_argument("--out", type=Path, default=Path("output"))
    ap.add_argument("--delay", type=float, default=1.0, help="pausa entre requisicoes (s)")
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = sem limite")
    ap.add_argument("--section", action="append", default=[],
                    help="restringe a uma secao da sidebar (repetivel), ex.: Gear")
    ap.add_argument("--expand-leaves", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignora paginas ja salvas")
    ap.add_argument("--site-root", default="https://stoneshard.com")
    ap.add_argument("--wiki-path", default="/wiki")
    ap.add_argument("--user-agent")
    a = ap.parse_args(argv)

    cfg = Config(site_root=a.site_root, wiki_path=a.wiki_path, out_dir=a.out, delay=a.delay,
                 max_depth=a.max_depth, max_pages=a.max_pages, only_sections=a.section,
                 expand_leaves=a.expand_leaves, download_images=not a.no_images,
                 refresh=a.refresh)
    if a.user_agent:
        cfg.user_agent = a.user_agent
    a.out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(a.out / "crawl.log", encoding="utf-8")])
    try:
        Crawler(cfg).run()
    except KeyboardInterrupt:
        log.warning("interrompido; paginas ja salvas em %s/pages permitem retomar", a.out)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
