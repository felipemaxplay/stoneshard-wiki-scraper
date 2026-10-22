#!/usr/bin/env python3
"""
Stoneshard Wiki -> base de dados local em JSON (+ imagens).      [patch 1.3]

Fluxo:
  1. Detecta o api.php da wiki (MediaWiki 1.45.x em stoneshard.com/wiki).
  2. Semeia a fila BFS com as SECOES da pagina inicial (Character, Gear, Consumables...),
     que viram o 1o item do breadcrumb; a sidebar e usada apenas como fallback.
  3. Para cada pagina: action=parse -> HTML renderizado + wikitext + categorias.
       - infobox   -> dict chave/valor em dados_estruturados (ver parse_infobox)
       - tabelas   -> lista de objetos em dados_estruturados["tabelas"] (ver parse_tables):
                      * "tabela": cabecalho vira chave (ate cabecalho so de icones)
                      * "grade" : grade de icones/links (ex.: pagina Weapons)
       - templates -> parametros em _wikitext_templates (no topo do registro, nao em
                      dados_estruturados); templates de ruido (cores, placeholders) sao descartados
       - imagens de infobox, de celulas de tabela e do corpo sao baixadas; so o
         caminho local e gravado no JSON
  4. Paginas com infobox ou ficha de skill (itens) sao "folhas": nao expandimos seus links.
  5. Grava output/pages/<titulo>.json (permite retomar) e output/stoneshard_db.json.
  6. Pai de breadcrumb: parent_overrides.json (manual, vence sempre) e depois a tabela-pai.

Uso:
  python stoneshard_scraper.py --max-pages 30            # teste curto
  python stoneshard_scraper.py --section Gear            # so a secao Gear
  python stoneshard_scraper.py --page Swords --page "Arna's Sword" --out out_test
                                                         # so estas paginas + dump HTML/wikitext
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
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import mwparserfromhell
import requests
from bs4 import BeautifulSoup, Comment, NavigableString
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("stoneshard")

# Versao do formato dos registros. Registros salvos com outra versao sao
# reprocessados (o cache de output/pages e invalidado automaticamente).
SCHEMA_VERSION = 4

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
    only_pages: list[str] = field(default_factory=list)   # modo --page
    expand_leaves: bool = False      # True = tambem segue links de paginas de item
    download_images: bool = True
    refresh: bool = False            # ignora cache de paginas ja salvas
    refine_breadcrumbs: bool = True  # pai = pagina cuja tabela lista a pagina (ver refine_breadcrumbs)
    parent_overrides: Path = Path("parent_overrides.json")   # pai manual (vence a heuristica)
    home_label: str = "Stoneshard Wiki"
    user_agent: str = ("StoneshardOfflineArchiver/1.1 "
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
# Subpaginas de template (tooltips etc.) e titulos com sintaxe de wikitext vazada
TECH_SUBPAGE = re.compile(r"/(tooltip|doc|sandbox|testcases)$", re.I)
BAD_TITLE_CHARS = re.compile(r"[{}|\[\]<>#]")
NON_PAGE_PREFIXES = ("images/", "resources/", "extensions/", "skins/", "api.php",
                     "index.php", "load.php")
NOISE_SELECTORS = [".navbox", ".toc", "#toc", ".mw-editsection", ".ambox", ".mbox-small",
                   ".mw-message-box", ".metadata", ".noprint", ".catlinks", "style",
                   "script", "sup.reference"]
NAV_TABLE_TITLES = {"items", "languages"}      # tabelas de navegacao sem classe CSS
SEE_ALSO_TITLES = {"see also", "references", "external links"}
SIDEBAR_SKIP = {"navigation", "search", "toolbox", "languages"}
# Templates de wikitext que nao carregam dados de conteudo
SKIP_TEMPLATES = {"stub", "navbox", "languages", "lang", "items", "see also", "main",
                  "clear", "reflist", "notice", "ambox", "toc", "dablink", "about"}
# Templates de ruido: cores (C, C#2...), largura (W), placeholders ({{{1|...}}}), navegacao
NOISE_TEMPLATE = re.compile(r"^(?:C|c|W|cl)(?:#\d+)?$|^Template:|^\{\{\{")
# Colunas cujo conteudo deve ser lido como lista de modificadores (chave: valor)
STAT_HEADERS = ("propert", "effect", "stat", "bonus", "modif", "attribute", "bonuses")
# Classificacoes que identificam uma skill (ativa ou passiva)
SKILL_CLASSES = {"passive", "attack", "maneuver", "spell", "stance"}
# Pai manual de breadcrumb. Entradas de parent_overrides.json somam-se a estas e vencem.
DEFAULT_PARENT_OVERRIDES = {
    "Strength": "Attributes & Stats",
    "Agility": "Attributes & Stats",
    "Perception": "Attributes & Stats",
    "Level": "Attributes & Stats",
    "Experience": "Attributes & Stats",
}

META_RE = re.compile(r"(Page last edited during patch|Data Page last updated in):\s*([\w.\-]+)")
TIER_RE = re.compile(r"^Tier\s+(\d+)\b\s*(.*)$", re.I)
NUM_LABEL_RE = re.compile(r"^([+\-−]?\d+(?:[.,]\d+)?%?)\s+([A-Za-z][A-Za-z' ]+)$")
INLINE_NUMLABEL_RE = re.compile(
    r"([+\-−]\d+(?:[.,]\d+)?%?)\s+([A-Za-z][A-Za-z' ]*?)(?=\s+[+\-−]\d|\s*$)")
DAMAGE_LINE_RE = re.compile(r"^(?:\d+(?:\.\d+)?\s+[A-Za-z]+\s+Damage\s*)+$", re.I)
PAIR_RE = re.compile(r"([A-Za-z][A-Za-z '’/&-]*?):\s*([+\-−]?\d[\d.,/]*%?)")
THUMB_RE = re.compile(r"/images/thumb/([0-9a-f]/[0-9a-f]{2})/([^/]+)/[^/]+$")
THUMB_W_RE = re.compile(r"/(\d+)px-[^/]+$")
NUMERIC_RE = re.compile(r"[+\-−]?\d[\d.,]*%?")
# Rotulo de bloco recolhivel: 'Show formulasHide formulas' (dois botoes colados).
# So casa o PAR Show/Hide, para nao apagar frases normais como 'Hide in Shadows'.
TOGGLE_PAIR_RE = re.compile(
    r"^(?:Show|Expand)\s+([A-Za-z ]{2,24}?)\s*(?:Hide|Collapse)\s+[A-Za-z ]{2,24}$")
TOGGLE_SINGLE_RE = re.compile(r"^(?:Show|Hide|Expand|Collapse)\s+[A-Za-z]{2,24}$")
STUB_RE = re.compile(r"^This article is a stub\b", re.I)

# Marcadores de icone dentro de linhas de texto (nao ocorrem em texto normal)
ICON_L, ICON_R = "⟦", "⟧"
ICON_RE = re.compile(f"{ICON_L}(.*?){ICON_R}")
TRAILING_ICON_VALUE_RE = re.compile(
    f"^(.*[^\\s\\d{ICON_R}])\\s+{ICON_L}([^{ICON_R}]+){ICON_R}\\s*([+\\-\\u2212]?\\d[\\d.,/]*%?)$")
BLOCK_TAGS = {"p", "div", "tr", "td", "th", "table", "ul", "ol", "li", "dl", "dt", "dd",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr", "caption", "thead",
              "tbody", "tfoot", "section", "center", "pre"}
# Tags que dao o 'tipo' da linha (titulo / item de lista); as demais contam como paragrafo
KIND_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dd", "dt"}
# '10 ⟦energia⟧ 8 ⟦cooldown⟧' ou '⟦Price⟧ 475': numero colado a um icone (qualquer ordem)
VALUE_ICON_RE = re.compile(
    f"(?:(\\d+(?:\\.\\d+)?%?)\\s*{ICON_L}([^{ICON_R}]+){ICON_R}"
    f"|{ICON_L}([^{ICON_R}]+){ICON_R}\\s*(\\d+(?:\\.\\d+)?%?))")
REQ_RE = re.compile(r"^-?\s*(Requires?\b.+)$")
# Rotulos curtos que aparecem SEM dois-pontos, com o valor na linha seguinte
# (ficha de skill: 'Type' / 'Target Object', 'Range' / '1', 'Modified by' / '...')
STAT_LABELS = {"type", "range", "modified by", "energy", "energy cost", "cooldown",
               "requirement", "requirements", "duration", "area", "radius", "target",
               "cast time", "charges", "uses"}
# Nomes de icones de ficha -> rotulo (iguala 'Energy'/'Cooldown' das tabelas de skills)
ICON_LABELS = {"energycost": "Energy", "energy cost": "Energy", "energy": "Energy",
               "cooldown": "Cooldown", "cooldown turns": "Cooldown"}
# Formula de atributos: '+(AGL + PRC)%', '+(STR)%', '(STR + 5)'
FORMULA_RE = re.compile(
    r"\(\s*[A-Z]{2,4}\s*(?:[+\-*/]\s*[A-Z0-9.]{1,4}\s*)*\)\s*%"
    r"|\(\s*[A-Z]{2,4}\s*(?:[+\-*/]\s*[A-Z0-9.]{1,4}\s*)+\)")
CORE_SKIP = ("Description", "Classification", "Extras")


# --------------------------------------------------------------------------- #
# Utilitarios de texto/titulo
# --------------------------------------------------------------------------- #

def norm(title: str) -> str:
    """Normaliza titulo MediaWiki: '_' -> ' ' e 1a letra maiuscula."""
    t = title.replace("_", " ").strip()
    return t[:1].upper() + t[1:]


def is_content_title(title: str) -> bool:
    if not title or BAD_TITLE_CHARS.search(title):
        return False
    if title.lower().startswith(NON_PAGE_PREFIXES) or LANG_SUBPAGE.search(title) \
            or TECH_SUBPAGE.search(title):
        return False
    if ":" in title and title.split(":", 1)[0].strip().lower() in IGNORED_NAMESPACES:
        return False
    return True


def clean(el: Any, sep: str = "") -> str:
    txt = el.get_text(sep) if hasattr(el, "get_text") else str(el)
    return re.sub(r"\s+", " ", txt.replace("\xa0", " ")).strip()


def convert(val: str) -> Any:
    """'80' -> 80, '1,250' -> 1250, '1.5' -> 1.5; '+5%' e 'Slashing: 17' continuam string."""
    v = val.strip().replace("−", "-")
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"\d{1,3}(,\d{3})+", v):
        return int(v.replace(",", ""))
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


def normalize_damage(text: str) -> str:
    """'17 Slashing Damage' / 'Slashing 17' / 'Slashing 25 Sacred 2' -> 'Slashing: 25, Sacred: 2'."""
    t = text.replace("−", "-").strip()
    if re.match(r"^\d", t):
        pairs = [(b, a) for a, b in re.findall(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", t)]
    else:
        pairs = re.findall(r"([A-Za-z]+)\s*:?\s*(\d+(?:\.\d+)?)", t)
    pairs = [(k, v) for k, v in pairs if k.lower() != "damage"]
    return ", ".join(f"{k}: {v}" for k, v in pairs) if pairs else text


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
        """Fallback: le MediaWiki:Sidebar -> {'Gear': ['Weapons', 'Armor', ...], ...}."""
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
# Parsing de HTML: base
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


def extract_links(soup: Any, cfg: Config) -> list[str]:
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        if "new" in (a.get("class") or []):          # redlink
            continue
        t = href_to_title(a["href"], cfg)
        if t and t not in out:
            out.append(t)
    return out


def home_sections(html: str, cfg: Config) -> dict[str, list[str]]:
    """
    Secoes da pagina inicial: cada <ul> de links recebe como rotulo o texto solto mais
    proximo antes dela ('Character', 'Gear', 'Consumables'...). Retorna {rotulo: [titulos]}.
    """
    soup = make_soup(html)
    sections: dict[str, list[str]] = {}
    for ul in soup.find_all("ul"):
        if ul.find_parent("ul"):
            continue
        titles = extract_links(ul, cfg)
        if not titles:
            continue
        node = ul.find_previous(
            string=lambda s: bool(s and s.strip()) and not isinstance(s, Comment)
            and not s.find_parent(["a", "li", "ul"]))
        label = clean(node) if node else ""
        if not label or len(label) > 40:
            label = cfg.home_label
        bucket = sections.setdefault(label, [])
        bucket.extend(t for t in titles if t not in bucket)
    return sections


def to_original_url(url: str) -> str:
    """/images/thumb/a/ab/File.png/120px-File.png -> /images/a/ab/File.png"""
    return THUMB_RE.sub(r"/images/\1/\2", url)


def img_name(img: Any) -> str:
    """Nome legivel do arquivo da imagem (usado quando nao ha alt)."""
    src = (img.get("src") or "").split("?")[0]
    name = unquote(to_original_url(src).rsplit("/", 1)[-1])
    name = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    return re.sub(r"[_\s]+", " ", name).strip()


def icon_like(img: Any) -> bool:
    """True para icones pequenos (<= 32px): ficam nas celulas, nao na lista da pagina."""
    try:
        if img.get("width") and int(str(img["width"]).rstrip("px")) <= 32:
            return True
    except ValueError:
        pass
    m = THUMB_W_RE.search((img.get("src") or "").split("?")[0])
    return bool(m and int(m.group(1)) <= 32)


# --------------------------------------------------------------------------- #
# Parsing de HTML: linhas logicas, infobox, tabelas
# --------------------------------------------------------------------------- #

def tag_flags(tag: Any) -> frozenset:
    """Pistas de visibilidade/formula de um elemento: 'formula', 'hidden', 'collapsible'."""
    cls = " ".join(tag.get("class") or []).lower()
    ident = (tag.get("id") or "").lower()
    style = re.sub(r"\s+", "", (tag.get("style") or "").lower())
    flags = set()
    if "formula" in cls or "formula" in ident:
        flags.add("formula")
    if "display:none" in style or "hidden" in cls or "mw-collapsed" in cls:
        flags.add("hidden")
    if "mw-collapsible" in cls or tag.get("data-expandtext") or tag.get("data-collapsetext"):
        flags.add("collapsible")
    return frozenset(flags)


def logical_lines_ex(el: Any) -> list[tuple[str, str, frozenset]]:
    """
    Quebra um elemento em 'linhas logicas' (tipo, texto, pistas): tags de bloco (div, tr, td,
    li, br...) separam linhas; tags inline (span, a, b, i...) sao concatenadas. Funciona com
    qualquer markup (div/span sem <p>/<li>/<table>). Imagens viram marcadores ⟦alt⟧ (alt ou
    nome do arquivo). 'tipo' e a tag mais proxima entre titulo (h1-h6) / li / blockquote,
    senao 'p'. 'pistas' reune as flags de tag_flags() dos ancestrais do texto.
    """
    out: list[tuple[str, str, frozenset]] = []
    buf: list[tuple[str, frozenset]] = []
    kinds: list[str] = []
    fstack: list[frozenset] = [frozenset()]

    def kind() -> str:
        for k in reversed(kinds):
            if k in KIND_TAGS:
                return k
        return "p"

    def flush() -> None:
        parts = list(buf)
        buf.clear()
        txt = re.sub(r"\s+", " ", "".join(t for t, _ in parts).replace("\xa0", " ")).strip()
        if txt and set(txt) - set("-–—|"):
            flags = frozenset().union(*(f for _, f in parts)) if parts else frozenset()
            out.append((kind(), txt, flags))

    def walk(node: Any) -> None:
        for c in node.children:
            if isinstance(c, Comment):
                continue
            if isinstance(c, NavigableString):
                buf.append((str(c), fstack[-1]))
                continue
            name = c.name
            if name in ("script", "style", "noscript"):
                continue
            if name == "img":
                alt = (c.get("alt") or "").strip() or img_name(c)
                buf.append((f" {ICON_L}{alt}{ICON_R} ", fstack[-1]))
                continue
            if name == "br":
                flush()
                continue
            block = name in BLOCK_TAGS
            if block:
                flush()
            kinds.append(name)
            fstack.append(fstack[-1] | tag_flags(c))
            walk(c)
            if block:
                flush()
            kinds.pop()
            fstack.pop()

    walk(el)
    flush()
    return out


def logical_lines(el: Any) -> list[str]:
    """Versao simples de logical_lines_ex: so os textos."""
    return [t for _, t, _ in logical_lines_ex(el)]


def undouble(text: str) -> str:
    """'Double LungeDouble Lunge' -> 'Double Lunge' (mesmo texto colado duas vezes, sem espaco)."""
    t = text.strip()
    half, odd = divmod(len(t), 2)
    if not odd and half >= 5 and t[:half] == t[half:]:
        return t[:half]
    return t


def icon_label(alt: str) -> str:
    """'Icon skill energycost' -> 'Energy'; 'Icon_skill_cooldown' -> 'Cooldown'; 'Price' fica."""
    a = alt.strip()
    if re.match(r"(?i)icon(?=[ _])", a):
        a = re.sub(r"(?i)^icon[ _]+(skill[ _]+)?", "", a).replace("_", " ").strip()
        return ICON_LABELS.get(a.lower(), a.title())
    return ICON_LABELS.get(a.lower(), a)


def resolve_icons(line: str) -> str:
    """
    ⟦Price⟧ 475            -> 'Price: 475'      (icone no inicio + valor numerico)
    17 ⟦Slashing⟧ Damage   -> '17 Slashing Damage'
    linha so de icones     -> ''
    """
    m = re.match(f"^{ICON_L}(.*?){ICON_R}\\s*(.*)$", line)
    if m:
        alt, rest = icon_label(m.group(1)), m.group(2).strip()
        if not rest or ICON_L in rest and not ICON_RE.sub("", rest).strip():
            return ""
        bare = ICON_RE.sub("", rest).strip()
        if alt and re.fullmatch(r"[+\-−]?\d[\d.,/]*%?", bare):      # icone + valor puro
            return f"{alt}: {bare}"
        # icone + texto: o alt so entra se ainda nao estiver escrito na linha
        line = rest if not alt or alt.lower() in rest.lower() else f"{alt} {rest}"

    plain = ICON_RE.sub(" ", line).lower()               # texto da linha sem marcadores

    def repl(mm: re.Match) -> str:
        alt = icon_label(mm.group(1))
        return f" {alt} " if alt and alt.lower() not in plain else " "
    return re.sub(r"\s+", " ", ICON_RE.sub(repl, line)).strip()


def interpret_lines(lines: list[str], title: str, attrs: dict) -> list[str]:
    """
    Converte linhas logicas em pares chave/valor dentro de `attrs`; devolve as sobras.
      'Chave:' + proxima linha      -> par          'Chave: valor' -> par
      'Tier 2 ...'                  -> Tier: 2      '17 Slashing Damage' -> Damage
      '+5% Armor Penetration'       -> Armor Penetration: '+5%'
    """
    expanded: list[str] = []
    for ln in lines:
        # '10 ⟦Icon skill energycost⟧ 8 ⟦Icon skill cooldown⟧' -> Energy: 10, Cooldown: 8
        pairs = VALUE_ICON_RE.findall(ln)
        rest_txt = re.sub(r"\bturns?\b", "", VALUE_ICON_RE.sub("", ln), flags=re.I)
        if pairs and not rest_txt.strip(" \t·,|/-"):
            for n1, a1, a2, n2 in pairs:
                put(attrs, icon_label(a1 or a2), convert(n1 or n2))
            continue
        # 'descricao... ⟦Price⟧ 475' -> 2 linhas
        m = TRAILING_ICON_VALUE_RE.match(ln)
        expanded += [m.group(1), f"{ICON_L}{m.group(2)}{ICON_R} {m.group(3)}"] if m else [ln]
    queue = [resolve_icons(ln) for ln in expanded]
    queue = [ln for ln in queue if ln]
    leftovers: list[str] = []
    i = 0
    while i < len(queue):
        ln = queue[i]
        i += 1
        if len(ln) > 80:
            leftovers.append(ln)
            continue
        if m := TIER_RE.match(ln):
            put(attrs, "Tier", int(m.group(1)))
            rest = m.group(2).strip()
            if rest:                                    # 'Tier 2 Arna's Sword Unique Sword'
                if title and rest.lower().startswith(title.lower()):
                    rest = rest[len(title):].strip()
                if rest:
                    queue.insert(i, rest)
            continue
        if (ln.rstrip(":").strip().lower() in STAT_LABELS and i < len(queue)
                and not queue[i].endswith(":")
                and queue[i].rstrip(":").strip().lower() not in STAT_LABELS):
            put(attrs, ln.rstrip(":").strip(), convert(queue[i]))     # 'Range' / '1'
            i += 1
            continue
        if m := REQ_RE.match(ln):                       # '- Requires a dagger'
            put(attrs, "Requirement", m.group(1))
            continue
        if ln.endswith(":") and i < len(queue) and not queue[i].endswith(":"):
            put(attrs, ln[:-1], convert(queue[i]))
            i += 1
            continue
        if ln.count(":") >= 2 and len(pairs := PAIR_RE.findall(ln)) >= 2:
            for k, v in pairs:
                put(attrs, k, convert(v))
            continue
        if ":" in ln:
            k, v = ln.split(":", 1)
            if v.strip() and len(k.split()) <= 4 and "." not in k:
                put(attrs, k, convert(v))
                continue
        if DAMAGE_LINE_RE.match(ln):
            dmg = normalize_damage(ln)
            attrs["Damage"] = f"{attrs['Damage']}, {dmg}" if "Damage" in attrs else dmg
            continue
        if len(inline := INLINE_NUMLABEL_RE.findall(ln)) >= 2:
            for num, label in inline:
                put(attrs, label.strip(), convert(num))
            continue
        if m := NUM_LABEL_RE.match(ln):
            put(attrs, m.group(2).strip(), convert(m.group(1)))
            continue
        leftovers.append(ln)
    return leftovers


def rows_of(table: Any) -> list[Any]:
    """<tr> que pertencem a esta tabela (exclui as de tabelas aninhadas)."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def find_infobox(root: Any) -> Any | None:
    for sel in ("table.infobox", ".infobox", ".portable-infobox", "table[class*=infobox]"):
        el = root.select_one(sel)
        if el:
            return el

    # Heuristica: tabela de topo, nao-wikitable, com imagem, >= 3 linhas e no maximo
    # 2 celulas por linha (formato 'ficha'). Tabelas de dados (3+ colunas) nao entram.
    def candidate(t: Any, allow_nested: bool) -> bool:
        if t.find_parent("table") or "wikitable" in (t.get("class") or []):
            return False
        if t.find("table") and not allow_nested:
            return False
        rows = rows_of(t)
        if len(rows) < 3 or not t.find("img") or len(t.find_all("a")) > 12:
            return False
        return max(len(r.find_all(["td", "th"], recursive=False)) for r in rows) <= 2

    for allow_nested in (False, True):
        for t in root.find_all("table"):
            if candidate(t, allow_nested):
                return t
    return None


def parse_infobox(box: Any, title: str) -> dict[str, Any]:
    """
    Extrai pares chave/valor de uma infobox de formato livre:
      a) linha <tr> com 2 celulas curtas -> chave/valor direto
      b) o resto vira linhas logicas e passa por interpret_lines()
      c) sobras: texto longo -> Description; 1a linha curta -> Classification; resto -> Extras
    """
    attrs: dict[str, Any] = {}
    for tr in rows_of(box):
        cells = tr.find_all(["th", "td"], recursive=False)
        if len(cells) == 2 and not tr.find("table") and not cells[0].find("img"):
            k = " ".join(resolve_icons(x) for x in logical_lines(cells[0])).strip()
            v = " ".join(resolve_icons(x) for x in logical_lines(cells[1])).strip()
            if k and v and len(k) <= 40 and len(v) <= 200:
                put(attrs, k.rstrip(":"), convert(v))
                tr.decompose()

    return attrs_from_lines(logical_lines(box), title, attrs)


def attrs_from_lines(lines: list[str], title: str, attrs: dict | None = None) -> dict[str, Any]:
    """Linhas logicas -> atributos (pares) + Description / Classification / Extras das sobras."""
    attrs = {} if attrs is None else attrs
    leftovers = interpret_lines(lines, title, attrs)
    leftovers = [ln for ln in leftovers if ln.lower() != title.lower()]
    desc = [ln for ln in leftovers if len(ln) > 40 or ln.endswith(".") or len(ln.split()) >= 6]
    short = [ln for ln in leftovers if ln not in desc]
    if desc:
        put(attrs, "Description", max(desc, key=len))
    if short:
        put(attrs, "Classification", short[0])
        if len(short) > 1:
            attrs["Extras"] = short[1:]
    dur = attrs.get("Durability")
    if isinstance(dur, str) and (m := re.fullmatch(r"(\d+)/(\d+)", dur)) and m.group(1) == m.group(2):
        attrs["Durability"] = int(m.group(1))            # 80/80 -> 80
    return attrs


def pair_extras(extras: list[str]) -> tuple[dict[str, Any], list[str]]:
    """
    Converte 'Extras' em pares rotulo/valor quando o valor e numerico:
      ['Backfire Chance', '10', 'Armor Penetration', '0%'] -> {'Backfire Chance': 10, ...}
    Devolve (pares, sobras).
    """
    pairs: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(extras):
        if (i + 1 < len(extras) and not NUMERIC_RE.fullmatch(extras[i])
                and NUMERIC_RE.fullmatch(extras[i + 1])):
            pairs[extras[i]] = convert(extras[i + 1])
            i += 2
        else:
            rest.append(extras[i])
            i += 1
    return pairs, rest


def normalize_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Padroniza formas do nivel de pagina: Description sempre lista, Extras como pares."""
    if isinstance(attrs.get("Description"), str):
        attrs["Description"] = [attrs["Description"]]
    if isinstance(attrs.get("Extras"), list):
        pairs, rest = pair_extras(attrs.pop("Extras"))
        for k, v in pairs.items():
            put(attrs, k, v)
        if rest:
            attrs["Extras"] = rest
    return attrs


def is_toggle_placeholder(text: str) -> bool:
    return bool(TOGGLE_SINGLE_RE.match(text))


def is_prose(text: str) -> bool:
    """Frase de texto corrido (e nao rotulo/valor de ficha)."""
    t = ICON_RE.sub("", text).strip()
    return len(t) > 80 or t.endswith((".", "!", "?")) or len(t.split()) >= 9


def is_heading(kind: str) -> bool:
    return len(kind) == 2 and kind[0] == "h" and kind[1].isdigit()


def leading_block(lines: list[tuple[str, str, frozenset]], title: str):
    """
    Ficha 'solta' (sem <table>): linhas curtas logo no inicio da pagina, antes da 1a frase de
    texto corrido ou titulo. Ex.: 'Double Lunge' / 'Attack' / '10 ⟦energia⟧ 8 ⟦cooldown⟧' /
    'Type' / 'Target Object' / 'Range' / '1'. Vale se render >= 2 atributos ou uma
    classificacao de skill (ex.: 'Passive'). Devolve (atributos, linhas_restantes).
    """
    seg: list[str] = []
    for kind, text, _ in lines:
        if is_heading(kind) or is_prose(text) or META_RE.search(text) or is_toggle(text, ()):
            break
        seg.append(text)
    if len(seg) < 2:
        return {}, lines
    attrs = attrs_from_lines(seg, title)
    core_n = len([k for k in attrs if k not in CORE_SKIP])
    if core_n < 2 and not is_skill_class(attrs.get("Classification")):
        return {}, lines
    return attrs, lines[len(seg):]


def is_skill_class(value: Any) -> bool:
    """'Passive' / 'Attack / Maneuver' -> True (classificacoes de skill)."""
    if not isinstance(value, str):
        return False
    return value.split(" / ")[0].strip().lower() in SKILL_CLASSES


def is_toggle(text: str, flags: Any) -> bool:
    """'Show formulasHide formulas' (par) ou 'Show formulas' dentro de bloco recolhivel."""
    t = ICON_RE.sub(" ", text).strip()
    return bool(TOGGLE_PAIR_RE.match(t) or ("collapsible" in flags and TOGGLE_SINGLE_RE.match(t)))


def toggle_label(text: str) -> str:
    m = TOGGLE_PAIR_RE.match(ICON_RE.sub(" ", text).strip())
    return m.group(1).strip() if m else ""


def is_formula(text: str, flags: Any, has_toggle: bool) -> bool:
    return ("formula" in flags or bool(FORMULA_RE.search(text))
            or (has_toggle and "hidden" in flags))


def split_description(head: list[tuple[str, str, frozenset]], has_toggle: bool):
    """
    Separa a descricao numerica da versao com formulas ('+(AGL + PRC)% Weapon Damage').
    Caso tipico da wiki: [numerica, resto, formula, resto] -> (metade 1, metade 2).
    Devolve (descricao, formulas), listas de frases.
    """
    texts = [re.sub(r"\s+", " ", ICON_RE.sub(" ", t)).strip() for _, t, _ in head]
    isf = [is_formula(t, f, has_toggle) for (_, _, f), t in zip(head, texts)]
    if not any(isf):
        return texts, []
    n = len(texts)
    if n >= 2 and n % 2 == 0:
        a, b, fb = texts[:n // 2], texts[n // 2:], isf[n // 2:]
        if not any(isf[:n // 2]) and any(fb) and all(x == y or f for x, y, f in zip(a, b, fb)):
            return a, b
    keep = list(dict.fromkeys(t for t, f in zip(texts, isf) if not f))
    return keep, [t for t, f in zip(texts, isf) if f]


# -- tabelas ------------------------------------------------------------------

def cell_texts(cell: Any) -> tuple[list[str], list[str]]:
    """(linhas de texto sem icones, nomes dos icones)."""
    texts, icons = [], []
    for ln in logical_lines(cell):
        icons += [x.strip() for x in ICON_RE.findall(ln) if x.strip()]
        t = undouble(re.sub(r"\s+", " ", ICON_RE.sub(" ", ln)).strip())
        if t:
            texts.append(t)
    return texts, icons


def header_name(cell: Any, idx: int) -> str:
    texts, icons = cell_texts(cell)
    if texts:
        return " ".join(texts)
    if icons:
        return re.sub(r"\s*icon$", "", " ".join(icons), flags=re.I).strip() or f"col{idx}"
    return f"col{idx}"


def shape_value(texts: list[str], icons: list[str], header: str) -> Any:
    """Decide a forma do valor de uma celula: numero, string, dict de modificadores ou lista."""
    h = header.lower()
    if not texts:
        return icons[0] if len(icons) == 1 else icons
    if "description" in h:                       # frases inteiras: nunca virar chave/valor
        return texts[0] if len(texts) == 1 else texts
    if "damage" in h:
        return normalize_damage(" ".join(texts))
    if h == "tier" and len(texts) == 1 and (m := TIER_RE.match(texts[0])):
        return int(m.group(1))
    if "basic" in h and len(texts) == 1 and not PAIR_RE.search(texts[0]):
        # 'Basic Stats' com so um rotulo ('Passive') vira objeto, nao string
        return {"Classification": texts[0]}
    stat_col = any(s in h for s in STAT_HEADERS)
    multi_pairs = len(texts) == 1 and len(PAIR_RE.findall(texts[0])) >= 2
    if len(texts) >= 2 or stat_col or multi_pairs:
        attrs: dict[str, Any] = {}
        left = interpret_lines(list(texts), "", attrs)
        if attrs and (stat_col or len(attrs) >= len(texts) / 2):
            if left and len(left[0]) <= 40 and not left[0].endswith("."):
                put(attrs, "Classification", left[0])     # 'Attack', 'Passive', 'Stance'
                left = left[1:]
            if left:
                attrs["_outros"] = left
            return attrs
    return convert(texts[0]) if len(texts) == 1 else texts


def expand_grid(rows: list[Any]) -> list[list[Any]]:
    """Alinha colunas aplicando colspan/rowspan: devolve matriz de celulas (<td>/<th>)."""
    def span(cell: Any, attr: str) -> int:
        try:
            return max(1, int(str(cell.get(attr, 1)).strip() or 1))
        except ValueError:
            return 1

    grid: list[list[Any]] = []
    carry: dict[int, tuple[int, Any]] = {}
    for tr in rows:
        row: list[Any] = []
        cells = iter(tr.find_all(["td", "th"], recursive=False))
        col = 0
        while True:
            if col in carry:
                rem, cell = carry[col]
                row.append(cell)
                carry[col] = (rem - 1, cell)
                if rem - 1 <= 0:
                    del carry[col]
                col += 1
                continue
            cell = next(cells, None)
            if cell is None:
                break
            rs = span(cell, "rowspan")
            for _ in range(span(cell, "colspan")):
                row.append(cell)
                if rs > 1:
                    carry[col] = (rs - 1, cell)
                col += 1
        grid.append(row)
    return grid


def parse_tables(root: Any, resolver: Callable[[Any], str | None], cfg: Config) -> list[dict]:
    """
    Converte cada tabela 'folha' em registros:
      tipo 'tabela': {"colunas": [...], "linhas": [{<cabecalho>: valor, "_paginas": [...],
                      "_imagens": [caminhos locais]}]}
      tipo 'grade' : {"celulas": [{"linha","coluna","secao","texto","paginas","imagens"}]}
    Imagens de dentro das celulas sao baixadas via `resolver`.
    """
    out: list[dict] = []
    memo: dict[int, tuple[list[str], list[str], list[str], list[str]]] = {}

    def analyse(cell: Any) -> tuple[list[str], list[str], list[str], list[str]]:
        key = id(cell)
        if key not in memo:
            texts, icons = cell_texts(cell)
            imgs = list(dict.fromkeys(p for im in cell.find_all("img") if (p := resolver(im))))
            pages = list(dict.fromkeys(
                t for a in cell.find_all("a", href=True)
                if "new" not in (a.get("class") or []) and (t := href_to_title(a["href"], cfg))))
            memo[key] = (texts, icons, imgs, pages)
        return memo[key]

    for t in root.find_all("table"):
        if t.decomposed or t.find("table"):
            continue                                     # so tabelas folha
        rows = rows_of(t)
        if not rows:
            continue
        grid = expand_grid(rows)
        ncols = max((len(r) for r in grid), default=0)
        cap = t.find("caption")
        head = t.find_previous(re.compile(r"^h[1-6]$"))
        titulo = clean(cap) if cap and clean(cap) else (clean(head) if head else "")
        all_cells = {id(c): c for r in grid for c in r}
        if len(all_cells) <= 1 or (len(grid) == 1 and ncols <= 3 and not t.find(["a", "img"])):
            continue                                     # caixas de citacao / layout trivial

        first_all_th = bool(grid[0]) and all(c.name == "th" for c in grid[0])
        th_only_body = any(r and all(c.name == "th" for c in r)
                           and len({id(c) for c in r}) >= 2 for r in grid[1:])
        is_data = first_all_th and ncols >= 2 and len(grid) >= 2 and not th_only_body

        if is_data:
            names, seen = [], {}
            for idx, c in enumerate(grid[0], 1):
                n = header_name(c, idx)
                seen[n] = seen.get(n, 0) + 1
                names.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
            linhas, secao = [], None
            for r in grid[1:]:
                distinct = list({id(c): c for c in r}.values())
                if len(distinct) == 1 and ncols > 1:    # linha-divisoria de secao
                    secao = " ".join(analyse(distinct[0])[0]) or secao
                    continue
                rec: dict[str, Any] = {}
                paginas: list[str] = []
                imagens: list[str] = []
                for name, c in zip(names, r):
                    texts, icons, imgs, pages = analyse(c)
                    val = shape_value(texts, icons, name)
                    if val not in ("", [], {}, None):
                        rec[name] = val
                    paginas += [p for p in pages if p not in paginas]
                    imagens += [p for p in imgs if p not in imagens]
                if len(r) != len(names):
                    rec["_aviso"] = "linha com numero de colunas diferente do cabecalho"
                if secao:
                    rec["_secao"] = secao
                if paginas:
                    rec["_paginas"] = paginas
                if imagens:
                    rec["_imagens"] = imagens
                if rec:
                    linhas.append(rec)
            if linhas:
                out.append({"titulo": titulo, "tipo": "tabela", "colunas": names,
                            "linhas": linhas})
        else:
            col_section: dict[int, str] = {}
            celulas: list[dict] = []
            for ri, r in enumerate(grid, 1):
                prev = None
                for ci, c in enumerate(r, 1):
                    if c is prev:                         # celula com colspan ja tratada
                        continue
                    prev = c
                    texts, icons, imgs, pages = analyse(c)
                    if c.name == "th" and texts:
                        for k in range(ci, ci + sum(1 for x in r if x is c)):
                            col_section[k] = " ".join(texts)
                        continue
                    if not (texts or imgs or pages):
                        continue
                    cel: dict[str, Any] = {"linha": ri, "coluna": ci}
                    if col_section.get(ci):
                        cel["secao"] = col_section[ci]
                    if texts:
                        cel["texto"] = " ".join(texts)
                    if pages:
                        cel["paginas"] = pages
                    if imgs:
                        cel["imagens"] = imgs
                    celulas.append(cel)
            if celulas:
                out.append({"titulo": titulo, "tipo": "grade", "celulas": celulas})
    return out


def prepare_lines(root: Any) -> tuple[list[tuple[str, str, frozenset]], dict[str, str], bool]:
    """
    Linhas logicas do corpo da pagina (qualquer markup), ja sem ruido:
    remove metadados ('Page last edited during patch: ...', devolvidos em `meta`), o aviso de
    stub e os botoes 'Show formulasHide formulas' (`has_toggle` avisa que existiam).
    """
    meta: dict[str, str] = {}
    out: list[tuple[str, str, frozenset]] = []
    has_toggle = False
    for kind, text, flags in logical_lines_ex(root):
        plain = re.sub(r"\s+", " ", ICON_RE.sub(" ", text)).strip()
        for k, v in META_RE.findall(plain):
            meta[k] = v
        if META_RE.sub("", plain).strip(" |") == "" and META_RE.search(plain):
            continue
        if STUB_RE.match(plain):
            continue
        if is_toggle(text, flags):
            has_toggle = True
            continue
        out.append((kind, text, flags))
    return out, meta, has_toggle


def render_text(lines: list[tuple[str, str, frozenset]]) -> str:
    """Linhas logicas -> texto limpo (titulos '## ', listas '- '); icones sao removidos."""
    rows: list[str] = []
    for kind, text, _ in lines:
        txt = re.sub(r"\s+", " ", ICON_RE.sub(" ", text)).strip(" |")
        if not txt:
            continue
        if is_heading(kind):
            rows.append(f"\n{'#' * int(kind[1])} {txt}")
        elif kind == "li":
            rows.append(f"- {txt}")
        else:
            rows.append(txt)
    return "\n".join(rows).strip()


def wikitext_templates(wikitext: str) -> dict[str, dict[str, Any]]:
    """Parametros dos templates de topo (mwparserfromhell), sem templates de ruido/navegacao."""
    out: dict[str, dict[str, Any]] = {}
    try:
        code = mwparserfromhell.parse(wikitext)
        for tpl in code.filter_templates(recursive=False):
            name = str(tpl.name).strip()
            if NOISE_TEMPLATE.match(name) or name.lower() in SKIP_TEMPLATES or name.startswith("#"):
                continue
            params = {}
            for p in tpl.params:
                val = mwparserfromhell.parse(str(p.value)).strip_code().strip()
                if val:
                    params[str(p.name).strip()] = convert(val)
            # Templates sem parametros ({{Skill}}) ficam como {}: mostram qual template gera a
            # pagina (os dados vem de uma Data Page / modulo, nao do wikitext).
            key, n = name, 2
            while key in out:                            # mesmo template repetido
                key, n = f"{name}#{n}", n + 1
            out[key] = params
    except Exception as exc:                             # noqa: BLE001 - wikitext atipico
        log.debug("wikitext_templates falhou: %s", exc)
    return out


# --------------------------------------------------------------------------- #
# Imagens
# --------------------------------------------------------------------------- #

class ImageStore:
    def __init__(self, cfg: Config, client: WikiClient):
        self.client = client
        self.dir = cfg.out_dir / "assets" / "img"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._memo: dict[str, str | None] = {}

    def fetch(self, original: str, fallback: str) -> str | None:
        """Baixa a imagem e devolve o caminho LOCAL relativo (nunca a URL)."""
        if original in self._memo:                       # tambem memoriza falhas
            return self._memo[original]
        name = safe_filename(unquote(urlparse(original).path.rsplit("/", 1)[-1]))
        dest = self.dir / name
        rel: str | None = f"assets/img/{name}"
        if not (dest.exists() and dest.stat().st_size > 0):
            rel = None
            for url in dict.fromkeys((original, fallback)):
                data = self.client.download(url)
                if data:
                    tmp = dest.with_name(dest.name + ".part")
                    tmp.write_bytes(data)
                    tmp.replace(dest)
                    rel = f"assets/img/{name}"
                    break
        self._memo[original] = rel
        return rel


# --------------------------------------------------------------------------- #
# Breadcrumbs: pai estrutural
# --------------------------------------------------------------------------- #

MAX_LISTING_ROWS = 200      # tabelas maiores que isso sao indices gerais, nao pais de verdade


def load_parent_overrides(path: Path) -> dict[str, str]:
    """Junta DEFAULT_PARENT_OVERRIDES com o arquivo do usuario (o arquivo vence)."""
    merged = dict(DEFAULT_PARENT_OVERRIDES)
    if path.exists():
        try:
            merged.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("nao consegui ler %s: %s", path, exc)
    return merged


def refine_breadcrumbs(records: list[dict], overrides: dict[str, str] | None = None) -> list[dict]:
    """
    O BFS monta o breadcrumb pelo 1o caminho que descobriu a pagina (ex.: 'Double Lunge' foi
    achada via 'Combat Formulas'). Regras, em ordem:
      1. parent_overrides: pai manual vence sempre.
      2. Caso contrario, o pai e a pagina cuja TABELA lista a pagina: prefere tabela (linhas)
         a grade, depois a maior listagem, depois o caminho mais curto.
    Guarda o antigo em 'breadcrumbs_origem'. Evita ciclos. Devolve os registros alterados.
    """
    overrides = overrides or {}
    by = {r["titulo"]: r for r in records}
    cand: dict[str, list[tuple]] = {}
    for r in records:
        for t in r.get("dados_estruturados", {}).get("tabelas", []):
            if t.get("tipo") == "tabela":
                rows, rank = t["linhas"], 0
                refs = [p for row in rows for p in row.get("_paginas", [])]
            else:
                rows, rank = t.get("celulas", []), 1
                refs = [p for c in rows for p in c.get("paginas", [])]
            if len(rows) > MAX_LISTING_ROWS:
                continue
            for p in dict.fromkeys(refs):
                if p in by and p != r["titulo"]:
                    cand.setdefault(p, []).append(
                        (rank, -len(rows), len(r["breadcrumbs"]), r["titulo"]))

    memo: dict[str, list[str]] = {}

    def crumbs(title: str, seen: frozenset) -> list[str]:
        if title in memo:
            return memo[title]
        if title in overrides:
            parent = overrides[title]
            if parent in by and parent not in seen and parent != title:
                result = crumbs(parent, seen | {title}) + [title]
            else:
                result = [parent, title] if parent != title else by[title]["breadcrumbs"]
        else:
            options = sorted(c for c in cand.get(title, []) if c[3] not in seen and c[3] != title)
            result = by[title]["breadcrumbs"]
            if options:
                result = crumbs(options[0][3], seen | {title}) + [title]
        memo[title] = result
        return result

    changed = []
    for r in records:
        new = crumbs(r["titulo"], frozenset())
        if new != r["breadcrumbs"]:
            r.setdefault("breadcrumbs_origem", r["breadcrumbs"])
            r["breadcrumbs"] = new
            changed.append(r)
    return changed


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

    # -- imagens ----------------------------------------------------------------
    def resolver(self) -> Callable[[Any], str | None]:
        """Funcao <img> -> caminho local (baixa na primeira vez). None se desativado/falhou."""
        cfg, store = self.cfg, self.images

        def resolve(img: Any) -> str | None:
            src = img.get("src") or ""
            if store is None or not src or src.startswith("data:"):
                return None
            full = urljoin(cfg.site_root + "/", src)
            orig = to_original_url(full)
            if unquote(urlparse(orig).path.rsplit("/", 1)[-1]) in cfg.ignore_images:
                return None
            return store.fetch(orig, full)
        return resolve

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
        main = self.client.parse_page(cfg.main_page)
        sections = home_sections(main["text"], cfg)
        if len([k for k in sections if k != cfg.home_label]) < 3:
            log.warning("poucas secoes na pagina inicial (%d); usando a sidebar", len(sections))
            sections = self.client.sidebar_sections() or sections
        log.info("secoes: %s", {k: len(v) for k, v in sections.items()})
        for label, targets in sections.items():
            if only and label.lower() not in only:
                continue
            for t in targets:
                self.enqueue(t, [label])
        if not only:
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
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return rec if rec.get("_schema") == SCHEMA_VERSION else None   # formato antigo: refaz

    # -- processamento de uma pagina -------------------------------------------
    def build_record(self, parsed: dict, parent_crumbs: list[str]) -> dict:
        cfg = self.cfg
        resolve = self.resolver()
        title = norm(parsed["title"])
        html = parsed["text"]
        soup = make_soup(html)
        children = extract_links(make_soup(html, drop_see_also=True), cfg)

        # 1) infobox -> atributos; a imagem principal vai para a lista da pagina
        images: list[str] = []
        attrs: dict[str, Any] = {}
        box = find_infobox(soup)
        if box is not None:
            for im in box.find_all("img"):
                if not icon_like(im) and (p := resolve(im)) and p not in images:
                    images.append(p)
            attrs = parse_infobox(box, title)
            box.decompose()

        # 2) tabelas -> lista de objetos (imagens das celulas sao baixadas aqui)
        tables = parse_tables(soup, resolve, cfg)
        for t in soup.find_all("table"):
            if not t.decomposed:
                t.decompose()

        # 3) imagens do corpo (fora de tabelas e sem icones)
        for im in soup.find_all("img"):
            if not icon_like(im) and (p := resolve(im)) and p not in images:
                images.append(p)

        # 4) corpo em linhas logicas (qualquer markup: div/span, nao so <p>/<li>)
        lines, meta, has_toggle = prepare_lines(soup)
        sheet: dict[str, Any] = {}
        if len([k for k in attrs if k not in CORE_SKIP]) < 2:
            # sem infobox em tabela: tenta a ficha 'solta' em linhas (paginas de skill)
            sheet, lines = leading_block(lines, title)
            for k, v in sheet.items():
                attrs.setdefault(k, v)
        core = [k for k in attrs if k not in CORE_SKIP]
        is_item = (len(core) >= 2 or "Requirement" in attrs
                   or is_skill_class(attrs.get("Classification")))

        # descricao: versao numerica x versao com formulas ('Show formulas')
        head_n = next((i for i, (kd, _, _) in enumerate(lines) if is_heading(kd)), len(lines))
        head, rest = lines[:head_n], lines[head_n:]
        desc, formulas = split_description(head, has_toggle)
        if formulas:
            attrs["Formulas"] = formulas
        if sheet and desc and "Description" not in attrs:
            attrs["Description"] = " ".join(desc)
        body = [("p", t, frozenset()) for t in desc]
        if formulas:
            body += [("h2", "Formulas", frozenset())] + [("p", t, frozenset()) for t in formulas]
        text = render_text(body + rest)

        # 5) metadados, tabelas e templates do wikitext (templates ficam no topo do registro)
        for k, v in meta.items():
            put(attrs, k, v)
        attrs = normalize_attrs(attrs)
        if tables:
            attrs["tabelas"] = tables
        tpls = wikitext_templates(parsed.get("wikitext", ""))
        if not text and parsed.get("wikitext"):
            try:
                text = mwparserfromhell.parse(parsed["wikitext"]).strip_code().strip()
            except Exception:                            # noqa: BLE001
                text = ""

        if is_item and not cfg.expand_leaves:
            children = []
        return {
            "_schema": SCHEMA_VERSION,
            "breadcrumbs": parent_crumbs + [title],
            "titulo": title,
            "conteudo_texto": text,
            "dados_estruturados": attrs,
            "_wikitext_templates": tpls,
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

    # -- modo de depuracao: so as paginas pedidas, com dump do HTML/wikitext --------
    def run_pages(self, titles: list[str]) -> None:
        dbg = self.cfg.out_dir / "debug"
        dbg.mkdir(exist_ok=True)
        overrides = load_parent_overrides(self.cfg.parent_overrides)
        for raw in titles:
            title = norm(raw)
            try:
                parsed = self.client.parse_page(title)
            except WikiApiError as exc:
                log.warning("pulando %s (%s)", title, exc)
                continue
            slug = re.sub(r"[^\w\-.'() ]+", "_", parsed["title"]).strip().replace(" ", "_")[:100]
            (dbg / f"{slug}.html").write_text(parsed["text"], encoding="utf-8")
            (dbg / f"{slug}.wikitext").write_text(parsed.get("wikitext", ""), encoding="utf-8")
            rec = self.build_record(parsed, [])                  # sem pai: o titulo ja e anexado
            real = norm(parsed["title"])
            if real in overrides:                                # pai manual vence
                rec["breadcrumbs"] = [overrides[real], real]
            self._page_file(real).write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            a = rec["dados_estruturados"]
            log.info("%s: tipo=%s atributos=%s tabelas=%s imagens=%d", rec["titulo"],
                     rec["tipo_pagina"], [k for k in a if not k.startswith("_") and k != "tabelas"],
                     [(t["tipo"], len(t.get("linhas", t.get("celulas", [])))) for t in
                      a.get("tabelas", [])], len(rec["imagens"]))
        log.info("dumps em %s e registros em %s", dbg, self.cfg.out_dir / "pages")

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
        if self.cfg.refine_breadcrumbs:
            overrides = load_parent_overrides(self.cfg.parent_overrides)
            changed = refine_breadcrumbs(self.records, overrides)
            for rec in changed:                       # mantem o cache de paginas coerente
                self._page_file(rec["titulo"]).write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("breadcrumbs refinados: %d paginas", len(changed))
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
                    help="restringe a uma secao da pagina inicial (repetivel), ex.: Gear")
    ap.add_argument("--page", action="append", default=[],
                    help="processa so esta pagina e salva dump do HTML/wikitext (repetivel)")
    ap.add_argument("--expand-leaves", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignora paginas ja salvas")
    ap.add_argument("--no-refine-breadcrumbs", action="store_true",
                    help="mantem o breadcrumb do 1o caminho descoberto (sem pai por tabela)")
    ap.add_argument("--parent-overrides", type=Path, default=Path("parent_overrides.json"),
                    help="JSON {pagina: pai} que vence a heuristica de breadcrumb")
    ap.add_argument("--site-root", default="https://stoneshard.com")
    ap.add_argument("--wiki-path", default="/wiki")
    ap.add_argument("--user-agent")
    a = ap.parse_args(argv)

    cfg = Config(site_root=a.site_root, wiki_path=a.wiki_path, out_dir=a.out, delay=a.delay,
                 max_depth=a.max_depth, max_pages=a.max_pages, only_sections=a.section,
                 only_pages=a.page, expand_leaves=a.expand_leaves,
                 download_images=not a.no_images, refresh=a.refresh,
                 refine_breadcrumbs=not a.no_refine_breadcrumbs,
                 parent_overrides=a.parent_overrides)
    if a.user_agent:
        cfg.user_agent = a.user_agent
    a.out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(a.out / "crawl.log", encoding="utf-8")])
    try:
        crawler = Crawler(cfg)
        if cfg.only_pages:
            crawler.run_pages(cfg.only_pages)
        else:
            crawler.run()
    except KeyboardInterrupt:
        log.warning("interrompido; paginas ja salvas em %s/pages permitem retomar", a.out)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())