"""
Testes offline dos parsers (sem rede). Rode:  python test_parsers.py
O HTML dos fixtures imita a estrutura observada nas paginas reais da Stoneshard Wiki.
"""
import json
from pathlib import Path

import stoneshard_scraper as s

CFG = s.Config(out_dir=Path("/tmp/_stoneshard_test"), download_images=False)


def fake_resolver(img):
    """Simula o download: devolve o caminho local que o ImageStore devolveria."""
    src = img.get("src") or ""
    name = s.unquote(s.to_original_url(src).rsplit("/", 1)[-1])
    return None if name in CFG.ignore_images else f"assets/img/{name}"


def record(html, title="Teste", crumbs=("Gear",), wikitext=""):
    c = s.Crawler.__new__(s.Crawler)
    c.cfg, c.images = CFG, None
    c.resolver = lambda: fake_resolver
    return c.build_record({"title": title, "text": html, "wikitext": wikitext,
                           "categories": [{"category": "Sword", "hidden": False}]}, list(crumbs))


# --------------------------------------------------------------------------- fixtures
INFOBOX_BLOCKS = """<div class="mw-parser-output">
<table><tr><td><div>Tier 2</div><div>Arna's Sword</div><div>Unique Sword</div></td></tr>
<tr><td><div><img alt="" src="/wiki/images/thumb/9/9c/Slashing.png/16px-Slashing.png"> 17 Slashing Damage</div></td></tr>
<tr><td><div><span>Armor Penetration:</span> <span>+5%</span></div><div><span>Block Chance:</span> <span>+7%</span></div>
<div><span>Block Power:</span> <span>+5</span></div><div><span>Fumble Chance:</span> <span>-3%</span></div>
<div><span>Durability:</span> <span>80/80</span></div></td></tr>
<tr><td>The surface of the blade bears an inscription, faded by time.<img alt="Price" src="/wiki/images/thumb/2/26/Gold_Icon.png/18px-Gold_Icon.png" width="18">475</td></tr>
<tr><td><img alt="Arna's Sword" src="/wiki/images/9/90/Arna%27s_Sword.png"></td></tr></table>
<h2>Acquired From</h2><p>Currently no merchant sells this item.</p>
<ul><li>Starting equipment for <a href="/wiki/Arna" title="Arna">Arna</a></li></ul>
<p><i>Page last edited during patch:</i> 0.9.3.9 | <i>Data Page last updated in:</i> 0.9.4.14</p></div>"""

# Variante: tudo inline na mesma celula (sem divs) e icone no meio da linha
INFOBOX_INLINE = """<table><tr><td><span>Tier 3</span> <span>Footman Sword</span> <span>Common Sword</span></td></tr>
<tr><td><span>21</span> <img alt="Slashing" src="/wiki/images/thumb/9/9c/Slashing.png/16px-Slashing.png"> <span>Damage</span></td></tr>
<tr><td><span>Armor Penetration: +10%</span> <span>Block Chance: +8%</span> <span>Durability: 100/100</span></td></tr>
<tr><td>A reliable blade used by the town guard.<img alt="Price" src="/wiki/images/thumb/2/26/Gold_Icon.png/18px-Gold_Icon.png">1,250</td></tr>
<tr><td><img alt="Footman Sword" src="/wiki/images/1/1a/Footman_Sword.png"></td></tr></table>"""

SWORDS_TABLE = """<div><h2>Swords</h2>
<table class="sortable"><tr>
<th>Tier</th><th>Item</th><th>Rarity</th>
<th><img alt="Damage" src="/wiki/images/thumb/1/11/Damage_Icon.png/20px-Damage_Icon.png"></th>
<th><img src="/wiki/images/thumb/4/44/Durability_icon.png/20px-Durability_icon.png"></th>
<th><img alt="Price" src="/wiki/images/thumb/2/26/Gold_Icon.png/18px-Gold_Icon.png"></th>
<th>Properties</th></tr>
<tr><td rowspan="2">Tier 2</td>
 <td><b><a href="/wiki/Arna%27s_Sword">Arna's Sword</a></b><a href="/wiki/Arna%27s_Sword"><img alt="Arna's Sword" src="/wiki/images/thumb/9/90/Arna%27s_Sword.png/40px-Arna%27s_Sword.png"></a></td>
 <td>Unique</td><td><img alt="" src="/wiki/images/thumb/9/9c/Slashing.png/16px-Slashing.png"> Slashing 17</td><td>80</td>
 <td><img alt="Gold" src="/wiki/images/thumb/2/26/Gold_Icon.png/18px-Gold_Icon.png"> 475</td>
 <td><div>Armor Penetration: +5%</div><div>Block Chance: +7%</div><div>Fumble Chance: -3%</div></td></tr>
<tr><td><a href="/wiki/Drifter_Sword">Drifter Sword</a><a href="/wiki/Drifter_Sword"><img alt="" src="/wiki/images/thumb/a/a1/Drifter_Sword.png/40px-Drifter_Sword.png"></a></td>
 <td>Common</td><td>Slashing 18</td><td>80</td><td>1,250</td>
 <td><span>+5% Armor Penetration</span> <span>+4% Block Chance</span></td></tr>
<tr><td>Tier 5</td><td><a href="/wiki/Radiant_Sword">Radiant Sword</a></td><td>Unique</td><td>Slashing 25 Sacred 2</td><td>140</td><td>5875</td>
 <td><div>Block Power Recovery: +1</div><div>Fatigue Resistance: +10%</div></td></tr>
</table></div>"""

WEAPONS_GRID = """<table>
<tr><th colspan="4">ONE-HANDED</th><th colspan="2">RANGED</th></tr>
<tr><td><a href="/wiki/Swords"><img alt="Swords" src="/wiki/images/thumb/5/5a/Swords.png/64px-Swords.png"></a></td>
    <td><a href="/wiki/Axes"><img alt="Axes" src="/wiki/images/thumb/6/6b/Axes.png/64px-Axes.png"></a></td><td></td><td></td>
    <td><a href="/wiki/Bows"><img alt="Bows" src="/wiki/images/thumb/7/7c/Bows.png/64px-Bows.png"></a></td><td></td></tr>
<tr><td><a href="/wiki/Swords">Swords</a></td><td><a href="/wiki/Axes">Axes</a></td><td></td><td></td><td><a href="/wiki/Bows">Bows</a></td><td></td></tr>
<tr><th colspan="4">TWO-HANDED</th><th colspan="2">AMMO</th></tr>
<tr><td><a href="/wiki/Spears"><img alt="Spears" src="/wiki/images/thumb/8/8d/Spears.png/64px-Spears.png"></a></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>"""

HOME = """<div><div class="x"><img src="/wiki/images/a/a1/Characters.png"></div><div class="title">Character</div>
<ul><li><a href="/wiki/Characters"><img src="/wiki/images/a/a2/c.png"></a> <a href="/wiki/Characters">Preset Characters</a></li>
<li><a href="/wiki/Traits">Traits</a></li><li><img src="/wiki/images/a/a3/lock.png"> Character Creation (TBA)</li></ul>
<div class="title">Gear</div>
<ul><li><a href="/wiki/Weapons">Weapons</a></li><li><a href="/wiki/Armor">Armor</a></li>
<li><a href="/wiki/Special:Random">r</a></li><li><a href="/wiki/File:Note1.png">f</a></li></ul>
<div class="title">Consumables</div><ul><li><a href="/wiki/Food">Food</a></li></ul></div>"""


# --------------------------------------------------------------------------- testes
def test_infobox_blocks():
    r = record(INFOBOX_BLOCKS, "Arna's Sword", ["Gear", "Weapons", "Swords"])
    d = r["dados_estruturados"]
    assert r["breadcrumbs"] == ["Gear", "Weapons", "Swords", "Arna's Sword"]
    assert r["tipo_pagina"] == "item" and r["filhos"] == []
    assert d["Tier"] == 2 and d["Classification"] == "Unique Sword"
    assert d["Damage"] == "Slashing: 17", d.get("Damage")
    assert d["Armor Penetration"] == "+5%" and d["Block Chance"] == "+7%"
    assert d["Block Power"] == 5 and d["Fumble Chance"] == "-3%"
    assert d["Durability"] == 80 and d["Price"] == 475
    assert d["Description"].startswith("The surface of the blade")
    assert d["Data Page last updated in"] == "0.9.4.14"
    assert r["imagens"] == ["assets/img/Arna's_Sword.png"], r["imagens"]   # sem icones
    assert "Acquired From" in r["conteudo_texto"]


def test_infobox_inline():
    d = record(INFOBOX_INLINE, "Footman Sword")["dados_estruturados"]
    assert d["Tier"] == 3 and d["Classification"] == "Common Sword"
    assert d["Damage"] == "Slashing: 21", d.get("Damage")
    assert d["Armor Penetration"] == "+10%" and d["Block Chance"] == "+8%"
    assert d["Durability"] == 100 and d["Price"] == 1250


def test_table_with_icon_headers():
    r = record(SWORDS_TABLE, "Swords", ["Gear", "Weapons"])
    assert r["tipo_pagina"] == "hub"
    tab = r["dados_estruturados"]["tabelas"][0]
    assert tab["tipo"] == "tabela" and tab["titulo"] == "Swords"
    assert tab["colunas"] == ["Tier", "Item", "Rarity", "Damage", "Durability", "Price", "Properties"]
    a, b, c = tab["linhas"]
    assert a["Tier"] == 2 and a["Item"] == "Arna's Sword" and a["Rarity"] == "Unique"
    assert a["Damage"] == "Slashing: 17" and a["Durability"] == 80 and a["Price"] == 475
    assert a["Properties"] == {"Armor Penetration": "+5%", "Block Chance": "+7%",
                               "Fumble Chance": "-3%"}
    assert a["_paginas"] == ["Arna's Sword"]
    assert "assets/img/Arna's_Sword.png" in a["_imagens"]            # imagem da celula
    assert b["Tier"] == 2                                            # rowspan propagado
    assert b["Price"] == 1250
    assert b["Properties"] == {"Armor Penetration": "+5%", "Block Chance": "+4%"}, b["Properties"]
    assert c["Damage"] == "Slashing: 25, Sacred: 2" and c["Tier"] == 5
    assert c["Properties"]["Fatigue Resistance"] == "+10%"
    assert "tabelas" in r["dados_estruturados"] and len(r["dados_estruturados"]["tabelas"]) == 1
    assert r["imagens"] == []          # icones/itens de tabela ficam nas linhas, nao na pagina


def test_icon_grid():
    tabs = record(WEAPONS_GRID, "Weapons")["dados_estruturados"]["tabelas"]
    assert len(tabs) == 1 and tabs[0]["tipo"] == "grade"
    cel = tabs[0]["celulas"]
    swords_icon = next(c for c in cel if c.get("imagens") == ["assets/img/Swords.png"])
    assert swords_icon["secao"] == "ONE-HANDED" and swords_icon["paginas"] == ["Swords"]
    assert any(c.get("texto") == "Bows" and c["secao"] == "RANGED" for c in cel)
    assert any(c.get("imagens") == ["assets/img/Spears.png"] and c["secao"] == "TWO-HANDED" for c in cel)
    assert all("texto" in c or "imagens" in c for c in cel)          # sem celulas vazias


def test_home_sections_and_titles():
    sec = s.home_sections(HOME, CFG)
    assert list(sec) == ["Character", "Gear", "Consumables"], list(sec)
    assert sec["Gear"] == ["Weapons", "Armor"]                       # sem Special:/File:
    assert sec["Character"] == ["Characters", "Traits"]
    for bad in ("{{#ifeq:{{SUBPAGENAME}}", "Wetness/Tooltip", "File:Note1.png", "Foo/ru", ""):
        assert not s.is_content_title(bad), bad
    assert s.is_content_title("Arna's Sword") and s.is_content_title("Swords (skill tree)")


def test_cache_schema(tmp=Path("/tmp/_stoneshard_cache")):
    tmp.mkdir(exist_ok=True)
    cfg = s.Config(out_dir=tmp, download_images=False)
    (tmp / "pages").mkdir(exist_ok=True)
    c = s.Crawler.__new__(s.Crawler)
    c.cfg = cfg
    c._page_file("X").write_text(json.dumps({"titulo": "X"}), encoding="utf-8")
    assert c._read_cache("X") is None                                # registro antigo e refeito
    c._page_file("X").write_text(json.dumps({"titulo": "X", "_schema": s.SCHEMA_VERSION}),
                                 encoding="utf-8")
    assert c._read_cache("X")["titulo"] == "X"


# --------------------------------------------------------------------------- patch 1.2
# Pagina de skill como a wiki real entrega: sem <table>/<p>; tudo em <div> com linhas soltas,
# rotulo e valor em linhas separadas, icones com numero, duas descricoes e botao Show/Hide.
SKILL_PAGE = """<div class="mw-parser-output">
<div>This article is a stub. You can help Stoneshard wiki by expanding it.</div>
<div class="skill-box">
 <div><img alt="" src="/wiki/images/thumb/1/1a/Double_Lunge.png/64px-Double_Lunge.png" width="64"></div>
 <div>Double Lunge</div><div>Attack</div>
 <div>10 <img alt="" src="/wiki/images/thumb/a/a1/Icon_skill_energycost.png/15px-Icon_skill_energycost.png"> 8 <img alt="" src="/wiki/images/thumb/b/b2/Icon_skill_cooldown.png/15px-Icon_skill_cooldown.png"></div>
 <hr>
 <div><div>Type</div><div>Target Object</div></div>
 <div><div>Range</div><div>1</div></div>
 <div><div>Modified by</div><div>Strength, Agility, Perception</div></div>
 <ul><li>Requires a dagger</li></ul>
</div>
<div class="desc"><div>Delivers two strikes with +20% Weapon Damage and +10% Bodypart Damage.</div>
 <div>If the skill kills its target or both strikes deal damage, reduces the skill's cooldown by 3 turns.</div></div>
<div class="desc"><div>Delivers two strikes with +(AGL + PRC)% Weapon Damage and +(STR)% Bodypart Damage.</div>
 <div>If the skill kills its target or both strikes deal damage, reduces the skill's cooldown by 3 turns.</div></div>
<div class="mw-collapsible-toggle"><span>Show formulas</span><span>Hide formulas</span></div>
<p><i>Page last edited during patch:</i> 0.8.0.27 | <i>Data Page last updated in:</i> 0.9.4.14</p></div>"""

SKILL_TREE = """<h2>Skill List</h2><table class="wikitable"><tr><th>Skill</th><th>Basic Stats</th><th>Description</th></tr>
<tr><td><a href="/wiki/Quick_Hands"><img alt="" src="/wiki/images/thumb/q/qh/Quick_Hands.png/40px-Quick_Hands.png"></a><a href="/wiki/Quick_Hands">Quick Hands</a></td>
 <td><div>Passive</div><div>- Requires a dagger</div></td>
 <td><div>Successful dagger strikes reduce the ability tree's cooldowns by 1 turn.</div><div>The effect doesn't stack.</div></td></tr>
<tr><td><a href="/wiki/Double_Lunge"><img alt="Double Lunge" src="/wiki/images/thumb/1/1a/Double_Lunge.png/40px-Double_Lunge.png"></a><span>Double Lunge</span><span class="t">Double Lunge</span></td>
 <td><div>Attack</div><div>Type: Target Object</div><div>Range: 1</div><div>Energy: 10</div><div>Cooldown: 8</div>
     <div>Modified by: Strength, Agility, Perception</div><div>- Requires a dagger</div></td>
 <td><div>Delivers two strikes with +20% Weapon Damage and +10% Bodypart Damage.</div>
     <div>Gaping Wound: +33%</div></td></tr></table>"""


def test_skill_page_sheet_and_formulas():
    r = record(SKILL_PAGE, "Double Lunge", ["Weaponry Abilities", "Daggers (skill tree)"])
    d = r["dados_estruturados"]
    assert r["tipo_pagina"] == "item" and r["filhos"] == []
    assert d["Classification"] == "Attack", d
    assert d["Energy"] == 10 and d["Cooldown"] == 8, d
    assert d["Type"] == "Target Object" and d["Range"] == 1, d
    assert d["Modified by"] == "Strength, Agility, Perception", d
    assert d["Requirement"] == "Requires a dagger", d
    assert d["Description"] == ("Delivers two strikes with +20% Weapon Damage and +10% Bodypart "
                                "Damage. If the skill kills its target or both strikes deal "
                                "damage, reduces the skill's cooldown by 3 turns."), d["Description"]
    assert d["Formulas"][0] == ("Delivers two strikes with +(AGL + PRC)% Weapon Damage and "
                                "+(STR)% Bodypart Damage."), d["Formulas"]
    assert len(d["Formulas"]) == 2 and "cooldown by 3 turns" in d["Formulas"][1]
    assert d["Data Page last updated in"] == "0.9.4.14"
    txt = r["conteudo_texto"]
    assert "Show formulas" not in txt and "stub" not in txt.lower(), txt
    assert "## Formulas" in txt and "+(AGL + PRC)%" in txt and "+20% Weapon Damage" in txt
    assert r["imagens"] == ["assets/img/Double_Lunge.png"], r["imagens"]


def test_hub_with_prose_is_not_a_sheet():
    html = "<div><div>This page shows all Dagger skills.</div><div>Main focus: Bleeding</div><h2>Skill List</h2></div>"
    r = record(html, "Daggers (skill tree)")
    assert r["tipo_pagina"] == "hub" and "Classification" not in r["dados_estruturados"]
    assert "This page shows all Dagger skills." in r["conteudo_texto"]
    assert "Main focus: Bleeding" in r["conteudo_texto"]


def test_normal_sentences_with_show_hide_are_kept():
    r = record("<div><p>Hide in Shadows</p><p>Show your strength to the guards, or they will refuse you.</p></div>")
    assert "Hide in Shadows" in r["conteudo_texto"] and "Show your strength" in r["conteudo_texto"]


def test_skill_tree_table_names_and_cells():
    tab = record(SKILL_TREE, "Daggers (skill tree)")["dados_estruturados"]["tabelas"][0]
    quick, lunge = tab["linhas"]
    assert quick["Skill"] == "Quick Hands"
    assert lunge["Skill"] == "Double Lunge", lunge["Skill"]            # antes: 'Double LungeDouble Lunge'
    assert lunge["Basic Stats"] == {"Type": "Target Object", "Range": 1, "Energy": 10,
                                    "Cooldown": 8, "Modified by": "Strength, Agility, Perception",
                                    "Requirement": "Requires a dagger", "Classification": "Attack"}, lunge["Basic Stats"]
    assert quick["Basic Stats"] == {"Requirement": "Requires a dagger", "Classification": "Passive"}
    assert lunge["Description"] == ["Delivers two strikes with +20% Weapon Damage and +10% Bodypart Damage.",
                                    "Gaping Wound: +33%"]            # frases intactas, sem virar dict
    assert quick["Description"][1] == "The effect doesn't stack."
    assert lunge["_paginas"] == ["Double Lunge"] and lunge["_imagens"] == ["assets/img/Double_Lunge.png"]


def test_undouble():
    assert s.undouble("Double LungeDouble Lunge") == "Double Lunge"
    assert s.undouble("Gaping WoundGaping Wound") == "Gaping Wound"
    assert s.undouble("Bora Bora") == "Bora Bora" and s.undouble("Mama") == "Mama"
    assert s.undouble("Coup de Grace") == "Coup de Grace"


def test_refine_breadcrumbs():
    def rec(title, crumbs, paginas=None, tipo="tabela"):
        tabs = []
        if paginas:
            key = "linhas" if tipo == "tabela" else "celulas"
            rows = [{"_paginas": [p]} if tipo == "tabela" else {"paginas": [p]} for p in paginas]
            tabs = [{"titulo": "", "tipo": tipo, key: rows}]
        return {"titulo": title, "breadcrumbs": list(crumbs), "dados_estruturados": {"tabelas": tabs}}
    recs = [rec("Combat Formulas", ["Combat & Survival", "Combat Formulas"]),
            rec("Daggers (skill tree)", ["Weaponry Abilities", "Daggers (skill tree)"], ["Double Lunge"]),
            rec("Double Lunge", ["Combat & Survival", "Combat Formulas", "Double Lunge"]),
            rec("A", ["X", "A"], ["B"]), rec("B", ["X", "A", "B"], ["A"])]            # ciclo
    changed = s.refine_breadcrumbs(recs)
    lunge = recs[2]
    assert lunge["breadcrumbs"] == ["Weaponry Abilities", "Daggers (skill tree)", "Double Lunge"]
    assert lunge["breadcrumbs_origem"] == ["Combat & Survival", "Combat Formulas", "Double Lunge"]
    assert recs[0]["breadcrumbs"] == ["Combat & Survival", "Combat Formulas"]          # sem pai: intacto
    assert all(len(r["breadcrumbs"]) < 8 for r in recs) and changed        # ciclo nao explode


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print(json.dumps(record(INFOBOX_BLOCKS, "Arna's Sword", ["Gear", "Weapons", "Swords"]),
                     ensure_ascii=False, indent=2))
