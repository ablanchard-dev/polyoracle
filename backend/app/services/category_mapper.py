"""Category mapper — derive a market category from slug + question text.

v0.7.2 P0.4: Gamma's `closed=true&order=endDate` response leaves most
markets uncategorised. With ~95% of resolved markets in the truth pool
falling back to the conservative 5% fee buffer, the fee-aware gating is
effectively neutralised. This module restores category coverage by
keyword-matching slug, event_slug, and question text against a curated
taxonomy.

Public API:

    map_category(slug=None, event_slug=None, question=None, tags=None)
        -> tuple[str, str]  # (canonical_category, confidence_method)

The canonical category aligns with ``CATEGORY_FEE_BUFFERS`` in
``capital_allocator.py`` so callers can plug the result straight into
the fee model.
"""

from __future__ import annotations

import re
from typing import Iterable


# Canonical category labels we emit. Keep aligned with capital_allocator.
CANONICAL_CRYPTO = "Crypto"
CANONICAL_POLITICS = "Politics"
CANONICAL_GEOPOLITICAL = "Geopolitical"
CANONICAL_SPORTS = "Sports"
CANONICAL_FINANCE = "Finance"
CANONICAL_TECH = "Tech"
CANONICAL_ECONOMICS = "Economics"
CANONICAL_CULTURE = "Culture"
CANONICAL_WEATHER = "Weather"
CANONICAL_OTHER = "Other"
CANONICAL_UNKNOWN = "Uncategorised"


# Keyword groups → canonical categories. Order matters: the first match wins.
KEYWORD_GROUPS: list[tuple[str, list[str]]] = [
    (
        CANONICAL_CRYPTO,
        # tickers, protocols, FDV, on-chain primitives
        [
            r"\bbtc\b",
            r"\bbitcoin\b",
            r"\beth\b",
            r"\bethereum\b",
            r"\bsol\b",
            r"\bsolana\b",
            r"\bxrp\b",
            r"\bripple\b",
            r"\bdoge\b",
            r"\bcardano\b",
            r"\bada\b",
            r"\bavax\b",
            r"\bavalanche\b",
            r"\bpolygon\b",
            r"\bmatic\b",
            r"\bnft\b",
            r"\bnfts\b",
            r"\bfdv\b",
            r"\btoken\b",
            r"\btokens\b",
            r"\btvl\b",
            r"\bdefi\b",
            r"\bstablecoin\b",
            r"\busdc\b",
            r"\busdt\b",
            r"\bdex\b",
            r"\bcefi\b",
            r"\bcrypto\b",
            r"\bblockchain\b",
            r"\bairdrop\b",
            r"\bonchain\b",
            r"\bweb3\b",
            r"\bcoinbase\b",
            r"\bbinance\b",
            r"\bkraken\b",
            r"\buniswap\b",
            r"\baave\b",
            r"\bcurve\b",
            r"\bmake\sup\sof\stoken\b",
            r"\bmainnet\b",
            r"\btestnet\b",
            r"\bmemecoin\b",
            r"\bvitalik\b",
            r"\bsec\sapprove\setf\b",
            r"\bspot\setf\b",
            # Common 5-min "X Up or Down" pattern + extra crypto names.
            r"\bup\sor\sdown\b",
            r"\bhyperliquid\b",
            r"\bbnb\b",
            r"\bdogecoin\b",
            r"\bdo\skwon\b",
            r"\bsui\b",
            r"\bapt\b",
            r"\baptos\b",
            r"\barb\b",
            r"\barbitrum\b",
            r"\bop\b",
            r"\boptimism\b",
            r"\bnear\sprotocol\b",
            r"\binj\b",
            r"\bblur\b",
            r"\bpepe\b",
            r"\bshiba\b",
            r"\bshib\b",
            r"\bhbar\b",
            r"\btrx\b",
            r"\btron\b",
            r"\bxlm\b",
            r"\bstellar\b",
            r"\bdot\b",
            r"\bpolkadot\b",
            r"\blink\b",
            r"\bchainlink\b",
            r"\bltc\b",
            r"\blitecoin\b",
            r"\bbch\b",
            r"\bbitcoin\scash\b",
            r"\bworldcoin\b",
            r"\busdt\b",
            r"\bcomet\b",
            r"\bsbf\b",
            r"\bftx\b",
            r"\bmica\b",
            r"\bsec\b",
        ],
    ),
    (
        CANONICAL_POLITICS,
        [
            r"\belection\b",
            r"\belections\b",
            r"\btrump\b",
            r"\bbiden\b",
            r"\bharris\b",
            r"\bdesantis\b",
            r"\bobama\b",
            r"\bsenator\b",
            r"\bcongress\b",
            r"\bsupreme\scourt\b",
            r"\bscotus\b",
            r"\bgop\b",
            r"\bdemocrat\b",
            r"\brepublican\b",
            r"\bprimary\b",
            r"\bpoll\b",
            r"\bcaucus\b",
            r"\bgubernatorial\b",
            r"\bgovernor\b",
            r"\bvice\spresident\b",
            r"\bus\spresident\b",
            r"\bpresidential\b",
            r"\bspeaker\sof\sthe\shouse\b",
            r"\bimpeach\b",
            r"\bimpeachment\b",
            r"\bnominee\b",
            r"\bvote\b",
            r"\bvoting\b",
            r"\bswing\sstate\b",
            r"\bferal\b",
            r"\bfederal\sreserve\b",
            r"\bus\ssenate\b",
            r"\bus\shouse\b",
            r"\bcabinet\b",
            r"\bsecretary\b",
            # Specific districts / state-level races
            r"\bhouse\sdistrict\b",
            r"\bcongressional\sdistrict\b",
            r"\b[a-z]{2}-\d+\sdemocratic\b",
            r"\b[a-z]{2}-\d+\srepublican\b",
            r"\bdemocratic\shouse\b",
            r"\brepublican\shouse\b",
            r"\bdemocratic\sprimary\b",
            r"\brepublican\sprimary\b",
            r"\bsenate\sdistrict\b",
        ],
    ),
    (
        CANONICAL_GEOPOLITICAL,
        [
            r"\bwar\b",
            r"\bukraine\b",
            r"\brussia\b",
            r"\bputin\b",
            r"\bzelensky\b",
            r"\bnato\b",
            r"\bmissile\b",
            r"\bisrael\b",
            r"\bgaza\b",
            r"\bhamas\b",
            r"\biran\b",
            r"\bhezbollah\b",
            r"\bnorth\skorea\b",
            r"\btaiwan\b",
            r"\bchina\b",
            r"\bxi\sjinping\b",
            r"\beu\b",
            r"\beurope\b",
            r"\bbrexit\b",
            r"\bunited\snations\b",
            r"\bun\ssecurity\b",
            r"\binvasion\b",
            r"\bcoup\b",
            r"\bsanctions\b",
        ],
    ),
    (
        CANONICAL_SPORTS,
        [
            r"\bnba\b",
            r"\bnfl\b",
            r"\bmlb\b",
            r"\bnhl\b",
            r"\bf1\b",
            r"\bformula\s1\b",
            r"\bsuper\sbowl\b",
            r"\bworld\sseries\b",
            r"\bworld\scup\b",
            r"\bnba\splayoffs\b",
            r"\bnfl\splayoffs\b",
            r"\bchampions\sleague\b",
            r"\bpremier\sleague\b",
            r"\bla\sliga\b",
            r"\bbundesliga\b",
            r"\bserie\sa\b",
            r"\btennis\b",
            r"\bgolf\b",
            r"\bpga\b",
            r"\bukbo\b",
            r"\bmma\b",
            r"\bufc\b",
            r"\bboxing\b",
            r"\bolympics\b",
            r"\bolympic\b",
            r"\beuro\s2024\b",
            r"\beuro\s2026\b",
            r"\bfifa\b",
            r"\bchess\b",
            r"\blakers\b",
            r"\bwarriors\b",
            r"\bcelltics\b",
            r"\bceltics\b",
            r"\bchiefs\b",
            r"\beagles\b",
            r"\bman\sutd\b",
            r"\bman\scity\b",
            r"\bbarcelona\b",
            r"\breal\smadrid\b",
            # Generic sports betting patterns — capture X-vs-Y matchups,
            # over/under, tournaments, head coach selections, etc.
            r"\bo/u\b",
            r"\bover/under\b",
            r"\bover\sunder\b",
            r"\bvs\.\s\w+",  # "vs. <Team>"
            r"\bvs\s+\w+\s+\w+",  # "vs Team Name"
            r"\bend\sin\sa\sdraw\b",
            r"\bbe\sa\sdraw\b",
            r"\bhead\scoach\b",
            r"\btournament\b",
            r"\btournment\b",
            r"\bopen\smen[’']s\b",
            r"\bopen\swomen[’']s\b",
            r"\bopen\sdoubles\b",
            r"\bopen\smixed\b",
            r"\bplayoff\b",
            r"\bplayoffs\b",
            r"\brebound[s]?\sover\b",
            r"\brebound[s]?\sunder\b",
            r"\bhome\srun[s]?\b",
            r"\bgrand\sslam\b",
            r"\bset\sscore\b",
            r"\bgame\s\d+\b",
            r"\btotal\skill[s]?\b",
            r"\bmap\shandicap\b",
            r"\bmarcus\sfreeman\b",
            r"\bmadrid\sopen\b",
            r"\bwimbledon\b",
            r"\bmasters\stournament\b",
            r"\bppa\b",
            r"\bppt\b",
            r"\bcoach\b",
            r"\bteam\b",
            r"\bgoal[s]?\sscored\b",
            r"\bppr\b",
            r"\bcurry\b",
            r"\blebron\b",
            r"\bjudge\b",
            r"\bharden\b",
            r"\bjordan\b",
            r"\boilers\b",
            r"\bducks\b",
            r"\bbruins\b",
            # Esports tournaments
            r"\biem\b",
            r"\besl\b",
            r"\bcsgo\b",
            r"\bcs\s2\b",
            r"\bdota\b",
            r"\bvalorant\b",
            r"\blol\b",
            r"\bleague\sof\slegends\b",
            r"\beternal\sfire\b",
            # Soccer teams (broader Euro / SA)
            r"\bbenfica\b",
            r"\bporto\b",
            r"\bspartak\b",
            r"\bzenit\b",
            r"\bred\sstar\b",
            r"\bhnk\b",
            r"\bdinamo\b",
            r"\bjuventus\b",
            r"\binter\smilan\b",
            r"\bnapoli\b",
            r"\bmilan\b",
            r"\bbayern\b",
            r"\bdortmund\b",
            r"\bpsg\b",
            r"\bajax\b",
            r"\bcd\b",
            r"\bset\shandicap\b",
            r"\bspread:\s",
            r"\biem\srio\b",
        ],
    ),
    (
        CANONICAL_FINANCE,
        [
            r"\bs&p\s500\b",
            r"\bsp500\b",
            r"\bdow\b",
            r"\bnasdaq\b",
            r"\binterest\srate\b",
            r"\binflation\b",
            r"\brecession\b",
            r"\bgdp\b",
            r"\bunemployment\b",
            r"\bjobs\sreport\b",
            r"\bcpi\b",
            r"\bppi\b",
            r"\bfed\srate\b",
            r"\brate\shike\b",
            r"\brate\scut\b",
            r"\bipo\b",
            r"\bearnings\b",
            r"\bmarket\scap\b",
            r"\bstock\sprice\b",
            r"\bipo\b",
            r"\bbond\syield\b",
            r"\bdollar\b",
            r"\beuro\b",
            r"\byen\b",
            # Indices Up/Down
            r"\bhang\sseng\b",
            r"\bnikkei\b",
            r"\bftse\b",
            r"\bdax\b",
            r"\bcac\b",
            r"\bvix\b",
            r"\bindex\sup\sor\sdown\b",
            r"\bhsi\b",
            r"\bspx\b",
            r"\bdjia\b",
            r"\bndx\b",
        ],
    ),
    (
        CANONICAL_TECH,
        [
            r"\bopenai\b",
            r"\bgpt\b",
            r"\bai\b",
            r"\bartificial\sintelligence\b",
            r"\bllm\b",
            r"\banthropic\b",
            r"\bgoogle\b",
            r"\bmeta\b",
            r"\bfacebook\b",
            r"\binstagram\b",
            r"\bapple\b",
            r"\biphone\b",
            r"\bmicrosoft\b",
            r"\bnvidia\b",
            r"\bspacex\b",
            r"\btesla\b",
            r"\bmusk\b",
            r"\belon\smusk\b",
            r"\btiktok\b",
            r"\byoutube\b",
            r"\btwitter\b",
            r"\bx\.com\b",
            r"\bgrok\b",
            r"\bclaude\b",
            r"\bgemini\b",
            r"\bwaymo\b",
        ],
    ),
    (
        CANONICAL_WEATHER,
        [
            r"\bhurricane\b",
            r"\btropical\sstorm\b",
            r"\btornado\b",
            r"\bblizzard\b",
            r"\bel\sni[nñ]o\b",
            r"\bla\sni[nñ]a\b",
            r"\bheatwave\b",
            r"\bdrought\b",
            r"\bflood\b",
            r"\bwildfire\b",
            r"\barctic\sblast\b",
        ],
    ),
    (
        CANONICAL_CULTURE,
        [
            r"\boscar[s]?\b",
            r"\bgrammy\b",
            r"\bemmy\b",
            r"\bgolden\sglobe\b",
            r"\bbillboard\b",
            r"\btime\sperson\sof\sthe\syear\b",
            r"\bswift\b",
            r"\btaylor\sswift\b",
            r"\bbeyonce\b",
            r"\bdrake\b",
            r"\bkanye\b",
            r"\bnetflix\b",
            r"\bdisney\b",
            r"\bmarvel\b",
            r"\bdcc\b",
            r"\bbox\soffice\b",
            r"\brotten\stomatoes\b",
        ],
    ),
    (
        CANONICAL_ECONOMICS,
        [
            r"\boil\sprice\b",
            r"\bgas\sprice\b",
            r"\bopec\b",
            r"\boil\b",
            r"\bnatural\sgas\b",
            r"\bgold\sprice\b",
            r"\bhousing\b",
            r"\bmortgage\b",
        ],
    ),
]


# Compile once at import.
COMPILED_GROUPS: list[tuple[str, list[re.Pattern[str]]]] = [
    (cat, [re.compile(p, re.IGNORECASE) for p in patterns])
    for cat, patterns in KEYWORD_GROUPS
]


def _normalise(*parts: str | None) -> str:
    chunks: list[str] = []
    for p in parts:
        if not p:
            continue
        chunks.append(str(p).replace("-", " ").replace("_", " "))
    return " ".join(chunks).lower()


def map_category(
    *,
    slug: str | None = None,
    event_slug: str | None = None,
    question: str | None = None,
    tags: Iterable[str] | None = None,
    existing_category: str | None = None,
) -> tuple[str, str]:
    """Return ``(canonical_category, method)``.

    method ∈ {EXISTING, TAG_MATCH, KEYWORD_MATCH, FALLBACK}.

    The function never raises. If nothing matches, returns
    ``(CANONICAL_UNKNOWN, "FALLBACK")``.
    """
    # 1) Honour an existing canonical category if it matches our taxonomy
    if existing_category:
        norm = existing_category.strip()
        if norm and norm.lower() not in {"uncategorised", "uncategorized", "none", "null"}:
            for cat, _ in KEYWORD_GROUPS:
                if cat.lower() == norm.lower():
                    return cat, "EXISTING"
            # non-canonical existing category — try to map common synonyms
            mapping = {
                "us-current-affairs": CANONICAL_POLITICS,
                "global politics": CANONICAL_GEOPOLITICAL,
                "ukraine & russia": CANONICAL_GEOPOLITICAL,
                "coronavirus": CANONICAL_OTHER,
                "pop-culture": CANONICAL_CULTURE,
                "pop culture": CANONICAL_CULTURE,
                "business": CANONICAL_FINANCE,
                "art": CANONICAL_CULTURE,
                "science": CANONICAL_TECH,
                "nba playoffs": CANONICAL_SPORTS,
                "nfl playoffs": CANONICAL_SPORTS,
                "olympics": CANONICAL_SPORTS,
                "chess": CANONICAL_SPORTS,
                "nfts": CANONICAL_CRYPTO,
            }
            mapped = mapping.get(norm.lower())
            if mapped:
                return mapped, "EXISTING"
            # Pass-through unknown
            return norm, "EXISTING"

    # 2) Tag match
    if tags:
        for tag in tags:
            tag_lower = (tag or "").strip().lower()
            for cat, patterns in COMPILED_GROUPS:
                if any(p.search(tag_lower) for p in patterns):
                    return cat, "TAG_MATCH"

    # 3) Keyword match on combined slug + event_slug + question
    haystack = _normalise(slug, event_slug, question)
    if haystack:
        for cat, patterns in COMPILED_GROUPS:
            if any(p.search(haystack) for p in patterns):
                return cat, "KEYWORD_MATCH"

    return CANONICAL_UNKNOWN, "FALLBACK"
