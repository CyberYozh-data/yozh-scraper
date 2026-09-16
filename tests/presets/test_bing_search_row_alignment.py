"""Guards for bing_search's organic-row selection across Bing's two `de` layouts.

BACKGROUND -- what the 2026-08-27 dual-engine audit measured: bing_search_chromium
returned 2 rows and 5 rows (row_alignment_mismatch both times) on `de`, against
10 and 10 for bing_search_camoufox from the same SERP, moments apart. The audit
report attributed this to an unpinned wait strategy (`research/preset_audit_
dual_engine_2026_08_27.json`, entries 24-25 for camoufox/de, 30-31 for
chromium/de). Two raw-HTML captures
taken seconds apart on the same `de` query (bing_camoufox_de.html, 122 KB;
bing_chromium_de.html, 638 KB) show a second, real cause this preset had
never matched -- but a first pass at this fix under-corrected it, and a
second pass left three claims unsupported by the very record they cited.
This module reflects the corrected, "fix round 2" understanding.

THE TWO SHAPES, verified directly against the captures (grep + lxml, and the
production `extract_fields` pipeline itself):

  CLASSIC (bing_camoufox_de.html): `#b_results` has 12 direct `<li>` children --
  ten `<li class="b_algo">` (one `<h2><a></a></h2>` each, real destination), plus
  `b_msg b_canvas` and `b_pag` (no `<h2>`, irrelevant chrome). No `div.b_wpt_bl`
  anywhere in this capture.

  COPILOT (bing_chromium_de.html): TWO organic-result shapes coexist, not one:

    1. `li.b_algo` x3 inside `#b_results` -- structurally identical to the
       classic shape.

    2. **`div.b_wpt_bl`** (ONE of four such divs, nested under
       `ol#b_topw.b_results_eml` inside Bing's Copilot answer widget) --
       byte-for-byte the SAME internal card template as `li.b_algo` (a
       `div.b_tpcn` favicon chip, `<h2><a target="_blank" href="...ck/a...
       &u=a1<base64>...">Title</a></h2>`, a `.b_caption p.b_lineclamp2`
       snippet). It decodes to `https://www.pcmag.com/picks/the-best-laptops`
       -- byte-identical title, destination and snippet to `li.b_algo[0]` of
       the CLASSIC capture. **Bing promotes the rank-1 organic result into
       the answer widget on this layout; it is still an organic result.**
       A first pass at this fix missed it entirely (returned 3 rows, not 4)
       because it only ever looked inside `#b_results`.

    The other three `div.b_wpt_bl` siblings in the SAME answer widget carry
    **zero** `<h2>` between them: one is a YouTube video card
    (`.mc_vtvc_link`), one is a "Mehr entdecken" topic-filter strip
    (`.magfilter-list-container`, a `<div role="heading">` standing in for a
    real `<h2>`), one is a `Gesponsert` ad card (`.mma_acf_ad`, an `aclk`
    href). All three carry real, resolvable `u=a1`-shaped or `aclk`-shaped
    hrefs -- the topic-filter strip's links decode to Bing's own
    `/shop/topics?q=...`, not an external destination -- but critically,
    **none of the three has any anchor inside an `<h2>`**, which is what a
    correct fix must key on (see "THE FIX" below), not "does this div merely
    contain a `u=a1` link anywhere" (a first attempt at the fix tried exactly
    that container-wide check and it match all three of these non-results
    too, on top of the real one).

    Inside `#b_results` itself: `li.b_ans.b_top` (Bing's generated-answer
    prose -- "The top laptops of 2026 include...", "Top Recommendations by
    Category", "Key Considerations for 2026 Laptops"), `li.b_ad.b_adBottom`
    (three nested ads, `Gesponsert`/`aclk`), and plain `li.b_ans` (a
    related-searches heading with no anchor at all). `li.b_ans.b_top` is
    **not** link-free -- it holds 22 real anchors decoding to 5 distinct
    external hosts (forbes.com, pcwelt.de, chip.de, faz.net,
    produkte-im-test.de) -- but every one of them is a numbered citation
    footnote (`a.md_citlink`, text like "Forbes" or a superscript number)
    sitting OUTSIDE its three `<h2>`s. Each of those three `<h2>`s -- checked
    directly -- contains **zero** anchors. That is the real, narrower reason
    an `h2`-scoped selector can never reach them, not "every anchor has
    `href=\"\"`" (an earlier, wrong claim: this element has no empty-href
    anchors either -- all 22 have real hrefs).

THE FIX: widen `titles`/`links` to also match `div.b_wpt_bl h2 a[href*=
'u=a1']` alongside `li.b_algo h2 a[href*='u=a1']` -- i.e. discriminate on the
ANCHOR carrying the organic `u=a1` redirect prefix, scoped to one living
inside an `<h2>`, not on the container class alone. `snippets`/
`result_blocks` mirror the same "b_algo OR (b_wpt_bl containing a qualifying
h2>a)" predicate via XPath (CSS has no `:has()`). This:
  - reaches the promoted PCMag result (its own h2>a carries `u=a1`);
  - leaves the three empty `div.b_wpt_bl` siblings alone (no `h2>a` at all,
    regardless of what other `u=a1`/`aclk` links float around inside them);
  - leaves `b_ans`/`b_ad` alone (no `b_algo`/`b_wpt_bl` class, so the
    container half of the predicate never matches them in the first place);
  - is a no-op on the classic capture (zero `div.b_wpt_bl` elements exist
    there at all).

Measured, production `extract_fields`, both twins' actual (now widened)
`parsing_instructions`, against both real captures:

    classic capture : titles=10 links=10 snippets=10 result_blocks=10, 0 warnings
    Copilot capture : titles=4  links=4  snippets=4  result_blocks=4,  0 warnings
                       (rank 1 = PCMag, matching the classic capture's own rank 1)

WHAT THIS FIX ESTABLISHES, WITH CERTAINTY, is the composition of the two
captures actually in hand: the classic capture's 10 `li.b_algo` and the
Copilot capture's 3 `li.b_algo` + 1 promoted `div.b_wpt_bl`, both verified
end-to-end with `extract_fields`, both complete, well-formed, final pages
(closing tags, pagination, the ad block, the related-searches heading all
present -- neither is a truncated mid-render snapshot). For these two pages
specifically, the row count is a final-state layout fact.

WHAT IT DOES NOT ESTABLISH, and the descriptions no longer claim, is that
the historical 2-row and 5-row runs (`research/preset_audit_dual_engine_
2026_08_27.json` entries 30-31) hit this same mechanism. Their raw HTML was
never kept (the audit record stores `result_blocks__slimmed_rows`, a count,
not markup), so their actual DOM cannot be inspected -- and the one piece of
entry 31 that CAN be checked argues against assuming it. Entry 31 was
collected under the OLD selector, `li.b_algo h2 a[href^='http']`, which
structurally cannot reach `div.b_wpt_bl` -- yet its own `titles[0]`/
`links[0]` is "The Best Laptops We've Tested (August 2026) | PCMag" /
"https://www.pcmag.com/picks/the-best-laptops". On the Copilot capture in
hand, PCMag exists ONLY inside `div.b_wpt_bl` -- there is no PCMag
`li.b_algo`. So whatever page entry 31 saw, it was not the "PCMag promoted
out of the list" shape this fix targets: PCMag was reachable there as an
ordinary `li.b_algo`. Entry 31's 5-row count -- itself also one snippet
short (`titles`=5, `snippets`=4, the same one-fewer-than-title/link pattern
as entry 30) -- is therefore AT LEAST as consistent with the wait-strategy
hypothesis the audit originally blamed (a page snapshotted mid-render, 5 of
10 classic slots painted so far, CHIP's caption not yet among them) as with
any Copilot-layout account of it. Both descriptions now say entries 30-31's
own layouts are unobserved, and treat entry 31's composition as evidence
AGAINST classifying it as Copilot rather than as a confirming data point.

One further consequence, not yet observed but not excludable either: entry
31 shows PCMag CAN render as a `li.b_algo`; the capture in hand shows PCMag
CAN be promoted OUT of `li.b_algo` entirely into `div.b_wpt_bl`. If a page
ever does BOTH at once -- renders the promoted card AND leaves its own
rank-1 result in `li.b_algo` -- this preset emits PCMag TWICE, once from
each half of the selector, arrays still equal length (5/5/5/5), zero
warnings. Co-occurrence has not been observed either way; both descriptions
name it as a known, unguarded possibility rather than a confirmed defect.

Also NOT claimed here, for the same reason: that which layout a session
draws correlates with engine at all. The two captures in hand are one
Copilot-layout observation on Chromium and one classic-layout observation on
Camoufox -- n=1 each, not n=3: the historical runs' layouts are unobserved
(previous paragraph), and counting entry 31 as a second Chromium/Copilot
data point is exactly the inference that paragraph retracts. No number is
put on any correlation, and no engine preference is asserted for `de` in
either direction.

extract_fields matches each field's selector against the whole document
independently and returns flat parallel arrays that a caller zips by index
(this preset's own description says as much), so the tests below check
alignment (equal lengths, in order) in addition to plain values -- and check
that non-organic content (ads, AI prose, the video/topic-strip siblings)
does NOT leak in, including through the `div.b_wpt_bl` container by itself.

Two more fixtures pin the `u=a1` discriminator directly, since it is
load-bearing and nothing above previously forced it to do any work: an ad
card whose title anchor sits inside an `<h2>` (so the h2 gate alone would
accept it -- only `u=a1` rejects it, since its href is a bare `aclk`
redirect) and a decoy whose href contains the bare substring `u=a1` without
the `[?&]` boundary character in front of it (so a selector written without
that boundary would accept it). Both are synthetic, not cut from any
capture -- exactly the condition under which a guard needs constructed
material, since nothing observed so far distinguishes the shipped selector
from either loosening.
"""
from __future__ import annotations

import base64
import json
import re
from html import unescape

import pytest

from src.extract.extractor import extract_fields
from src.extract.models import ExtractRule, FieldRule
from src.presets.models import Preset
from src.presets.store import DEFAULT_BUILTIN_DIR


def _load(name: str) -> Preset:
    path = DEFAULT_BUILTIN_DIR / f"{name}.json"
    return Preset(**json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# CLASSIC layout fixture. Cut verbatim from the real capture
# `bing_camoufox_de.html` (bing_search_camoufox, de locale, 2026-08-27):
# all ten `<li class="b_algo">` blocks, byte-for-byte, in their original order.
# The only edit: the first block's ~18 `<link rel="stylesheet">` preload tags
# (page-level asset preloads that Bing repeats verbatim ahead of the first
# result on the real page; irrelevant to every selector this preset uses)
# were dropped for readability.
# ---------------------------------------------------------------------------
_CLASSIC_ALGO_BLOCKS = [
    '<li class="b_algo" data-id="" iid="SERP.5343"><div class="b_tpcn"><a class="tilk" aria-label="pcmag.com" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=1b5bd53b6f60278c8081ebd85c720b3671704b3bd923466b72cc2c82679d199fJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cucGNtYWcuY29tL3BpY2tzL3RoZS1iZXN0LWxhcHRvcHM&amp;ntb=1" h="ID=SERP,5118.1"><div class="tptxt"><div class="tptt">pcmag.com</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=1b5bd53b6f60278c8081ebd85c720b3671704b3bd923466b72cc2c82679d199fJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cucGNtYWcuY29tL3BpY2tzL3RoZS1iZXN0LWxhcHRvcHM&amp;ntb=1" h="ID=SERP,5118.2">The <strong>Best Laptops</strong> We\'ve Tested (August <strong>2026</strong>) | PCMag</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">1. Aug. 2026</span> · Whether you want a simple budget PC, a productivity workhorse, or a screamer of a gaming notebook, our experts …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5344"><div class="b_tpcn"><a class="tilk" aria-label="chip.de" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=81419ab70fd2698c1f0c75a078eb33abcb0116df077a976a8fcb568c383bb3eeJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1" h="ID=SERP,5134.1"><div class="tptxt"><div class="tptt">chip.de</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=81419ab70fd2698c1f0c75a078eb33abcb0116df077a976a8fcb568c383bb3eeJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1" h="ID=SERP,5134.2"><strong>Notebook</strong>-Test.<strong>2026</strong>: Allround-<strong>Laptops</strong> auf dem Prüfstand - CHIP</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">19. Juli 2026</span> · Der Markt ist groß und unübersichtlich – mit unserem Notebook Test behalten Sie jederzeit den Überblick. Wir stellen …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5345"><div class="b_tpcn"><a class="tilk" aria-label="pcwelt.de" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=85cde8a0e6847679d32267e35bdc530d728efadd04ee03e190e6bbe24d9da5e6JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cucGN3ZWx0LmRlL2FydGljbGUvMjIxNTM4NS9kaWUtYmVzdGVuLWxhcHRvcHMtdGVzdC5odG1s&amp;ntb=1" h="ID=SERP,5151.1"><div class="tptxt"><div class="tptt">pcwelt.de</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=85cde8a0e6847679d32267e35bdc530d728efadd04ee03e190e6bbe24d9da5e6JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cucGN3ZWx0LmRlL2FydGljbGUvMjIxNTM4NS9kaWUtYmVzdGVuLWxhcHRvcHMtdGVzdC5odG1s&amp;ntb=1" h="ID=SERP,5151.2"><strong>Laptop</strong>-Test <strong>2026</strong>: Die besten Notebooks aller Klassen im Vergleich</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">Unser Notebook-Testvergleich 2026 bietet Ihnen einen kompakten Überblick und hilft, den besten Laptop für Ihre Bedürfnisse zu finden.</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5346"><div class="b_tpcn"><a class="tilk" aria-label="tomshardware.com" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=9dc3ac48959f8a384f18a3d63ea913d04a624d3a69a6c0440c002a920033bd1bJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cudG9tc2hhcmR3YXJlLmNvbS9sYXB0b3BzL2Jlc3QtbGFwdG9wcw&amp;ntb=1" h="ID=SERP,5166.1"><div class="tptxt"><div class="tptt">tomshardware.com</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=9dc3ac48959f8a384f18a3d63ea913d04a624d3a69a6c0440c002a920033bd1bJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cudG9tc2hhcmR3YXJlLmNvbS9sYXB0b3BzL2Jlc3QtbGFwdG9wcw&amp;ntb=1" h="ID=SERP,5166.2"><strong>Best Laptops 2026</strong>: Our benchmarked picks for productivity, …</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">The MacBook Air has been a go-to laptop recommendation for quite some time, thanks to strong performance, a fanless design, …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5347"><div class="b_tpcn"><a class="tilk" aria-label="produkte-im-test.de" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=5a4d95e115781568b6b97d620160f6e03af3a51e4c9abbd2c388518387905500JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly9wcm9kdWt0ZS1pbS10ZXN0LmRlL2xhcHRvcHMtaW0tdGVzdC1zdGlmdHVuZy13YXJlbnRlc3Qv&amp;ntb=1" h="ID=SERP,5184.1"><div class="tptxt"><div class="tptt">produkte-im-test.de</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=5a4d95e115781568b6b97d620160f6e03af3a51e4c9abbd2c388518387905500JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly9wcm9kdWt0ZS1pbS10ZXN0LmRlL2xhcHRvcHMtaW0tdGVzdC1zdGlmdHVuZy13YXJlbnRlc3Qv&amp;ntb=1" h="ID=SERP,5184.2"><strong>Laptop</strong> Testsieger <strong>2026</strong> | Stiftung Warentest - Produkte im Test</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">Vor 4 Tagen</span> · Im Folgenden stellen wir sowohl die aktualisierten Empfehlungen als auch die bisherigen Testsieger 2026 von Stiftung …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5348"><div class="b_tpcn"><a class="tilk" aria-label="faz.net" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=9f363d27fb8388635a5b3d9c55b0fed43747056c5811936e163bcfb524f018e0JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuZmF6Lm5ldC9rYXVma29tcGFzcy92ZXJnbGVpY2gvZGFzLWJlc3RlLWxhcHRvcC1mdWVyLWRpZS1tZWlzdGVuLw&amp;ntb=1" h="ID=SERP,5202.1"><div class="tptxt"><div class="tptt">faz.net</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=9f363d27fb8388635a5b3d9c55b0fed43747056c5811936e163bcfb524f018e0JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuZmF6Lm5ldC9rYXVma29tcGFzcy92ZXJnbGVpY2gvZGFzLWJlc3RlLWxhcHRvcC1mdWVyLWRpZS1tZWlzdGVuLw&amp;ntb=1" h="ID=SERP,5202.2">Das beste <strong>Laptop</strong> | Vergleich 08/<strong>2026</strong> | F.A.Z. Kaufkompass</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">8. Juli 2026</span> · Wir haben acht neue Laptops in unseren Vergleich aufgenommen. Unser neuer Favorit für die meisten ist das Lenovo …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5349"><div class="b_tpcn"><a class="tilk" aria-label="cnet.com" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=1fa9c937f71ea589b85edc59661eb6a7044b509f85bd72ddb4e73c20b5dfcf0fJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY25ldC5jb20vdGVjaC9jb21wdXRpbmcvYmVzdC1sYXB0b3Av&amp;ntb=1" h="ID=SERP,5219.1"><div class="tptxt"><div class="tptt">cnet.com</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=1fa9c937f71ea589b85edc59661eb6a7044b509f85bd72ddb4e73c20b5dfcf0fJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY25ldC5jb20vdGVjaC9jb21wdXRpbmcvYmVzdC1sYXB0b3Av&amp;ntb=1" h="ID=SERP,5219.2"><strong>Best Laptops</strong> of <strong>2026</strong>: <strong>Top</strong> Picks Tested by <strong>CNET</strong></a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">11. Aug. 2026</span> · These are the best laptops that my colleagues and I have gotten our hands on in the past year, spanning all types, …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5350"><div class="b_tpcn"><a class="tilk" aria-label="forbes.com" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=05d9e3b0384d62fdee6e6f45d056268d778febed49a85925d5ed4ee0ebfff0bbJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuZm9yYmVzLmNvbS9zaXRlcy9mb3JiZXMtcGVyc29uYWwtc2hvcHBlci9hcnRpY2xlL2Jlc3QtbGFwdG9wLw&amp;ntb=1" h="ID=SERP,5235.1"><div class="tptxt"><div class="tptt">forbes.com</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=05d9e3b0384d62fdee6e6f45d056268d778febed49a85925d5ed4ee0ebfff0bbJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuZm9yYmVzLmNvbS9zaXRlcy9mb3JiZXMtcGVyc29uYWwtc2hvcHBlci9hcnRpY2xlL2Jlc3QtbGFwdG9wLw&amp;ntb=1" h="ID=SERP,5235.2"><strong>Best Laptops 2026</strong> - Forbes Vetted</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">15. Juni 2026</span> · Here are the best laptops you can buy today, for different types of consumers and use cases. We tested over 20 …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5351"><div class="b_tpcn"><a class="tilk" aria-label="mediamarkt.de" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=a876688504dade81bc2b9651735d1f0fb9271e02cd666a5cacfde34ae33dbdb6JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cubWVkaWFtYXJrdC5kZS9kZS9jb250ZW50L2l0LWluZm9ybWF0aWsvbm90ZWJvb2tzL2Jlc3RlLWxhcHRvcHMtaW4tdGVzdHM_bXNvY2tpZD0yMmI2Yjg5OWFjZDE2OWEwMThkOWFmNWFhZDk1NjhkOA&amp;ntb=1" h="ID=SERP,5252.1"><div class="tptxt"><div class="tptt">mediamarkt.de</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=a876688504dade81bc2b9651735d1f0fb9271e02cd666a5cacfde34ae33dbdb6JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cubWVkaWFtYXJrdC5kZS9kZS9jb250ZW50L2l0LWluZm9ybWF0aWsvbm90ZWJvb2tzL2Jlc3RlLWxhcHRvcHMtaW4tdGVzdHM_bXNvY2tpZD0yMmI2Yjg5OWFjZDE2OWEwMThkOWFmNWFhZDk1NjhkOA&amp;ntb=1" h="ID=SERP,5252.2"><strong>Laptops</strong> in Tests: Aktueller Vergleich <strong>2026</strong> - MediaMarkt</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">29. Apr. 2026</span> · Entdecke die besten Laptops für Gaming, Arbeit, Streaming &amp; Co. Jetzt die Top-Modelle in Laptop-Tests bei …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5352"><div class="b_tpcn"><a class="tilk" aria-label="wired.com" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=8b18c04d4f206f164c0026b196dc9c544472d229f60a79ebd08e1e14d64d5547JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cud2lyZWQuY29tL3N0b3J5L2Jlc3QtbGFwdG9wcy8&amp;ntb=1" h="ID=SERP,5268.1"><div class="tptxt"><div class="tptt">wired.com</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=8b18c04d4f206f164c0026b196dc9c544472d229f60a79ebd08e1e14d64d5547JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cud2lyZWQuY29tL3N0b3J5L2Jlc3QtbGFwdG9wcy8&amp;ntb=1" h="ID=SERP,5268.2"><strong>Best Laptops</strong> (<strong>2026</strong>): My <strong>Top</strong> Recommendations After Testing …</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">25. Juli 2026</span> · The Best Laptops I’ve tested laptops for more than a decade. From MacBooks to gaming laptops, these are the …</p></div></li>',
]

BING_CLASSIC_SERP = "<html><body><ol id=\"b_results\">" + "".join(_CLASSIC_ALGO_BLOCKS) + "</ol></body></html>"

# A RICH CARD result (bing.com `best laptop 2026`, cc=de, 2026-09-05): an
# organic `li.b_algo` whose caption holds a `b_richcard` tab strip and no `<p>`
# at all. The snippet does not exist for this row; a snippets selector that
# ends in `//p` drops the row instead of yielding null, and every snippet
# after it moves up one title.
_CHIP_ALGO_HEADER = (
    '<li class="b_algo" data-id="" iid="SERP.5344"><div class="b_tpcn"><a class="tilk" aria-label="chip.de" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=81419ab70fd2698c1f0c75a078eb33abcb0116df077a976a8fcb568c383bb3eeJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1">'
    '<div class="tptxt"><div class="tptt">chip.de</div></div></a></div>'
    '<h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=81419ab70fd2698c1f0c75a078eb33abcb0116df077a976a8fcb568c383bb3eeJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=22b6b899-acd1-69a0-18d9-af5aad9568d8&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1" h="ID=SERP,5134.2">'
    '<strong>Notebook</strong>-Test.<strong>2026</strong>: Allround-<strong>Laptops</strong> auf dem Prüfstand - CHIP</a></h2>'
)
BING_RICH_CARD_ALGO = (
    _CHIP_ALGO_HEADER
    + '<div class="b_caption b_stsp1"><div class="b_richcard"><div class="rc_herotabheader"><div class="tab-head">'
    '<ul class="tab-menu"><li class="tab-active">Testsieger</li><li>Preis-Leistung</li></ul></div></div></div></div></li>'
)
# A result whose caption is empty but whose deep links (`ul.b_vList`) carry
# `<p>` descriptions of their own (a shape the 2026-07-26 `us` capture has
# without the `<p>`). The snippet is the `b_lineclamp` paragraph or nothing:
# a sitelink's description must not be promoted to the result's snippet,
# because it reads as one and no consumer could tell.
BING_DEEP_LINKS_ALGO = (
    _CHIP_ALGO_HEADER
    + '<div class="b_caption b_rich"></div><ul class="b_vList b_divsec"><li><div class="b_deep">'
    '<h3><a href="https://www.chip.de/tests/notebooks">Notebook-Tests</a></h3>'
    '<p>Alle Notebook-Tests von CHIP im Überblick, sortiert nach Kategorie.</p></div></li></ul></li>'
)
BING_RICH_CARD_SERP = (
    "<html><body><ol id=\"b_results\">"
    + _CLASSIC_ALGO_BLOCKS[0] + BING_RICH_CARD_ALGO + _CLASSIC_ALGO_BLOCKS[2]
    + "</ol></body></html>"
)


# ---------------------------------------------------------------------------
# COPILOT layout fixture. Cut verbatim (or trimmed, each time documented) from
# `bing_chromium_de.html` (bing_search_chromium, de locale, 2026-08-27).
# ---------------------------------------------------------------------------

# The promoted rank-1 result. Byte-for-byte from the capture (only the
# per-block inline `<style>` -- Bing repeats page CSS ahead of the first
# `div.b_wpt_bl` too, same as it does for the first `li.b_algo` -- was
# dropped, same edit as the classic fixture's first block). Same internal
# template as `li.b_algo`, same PCMag destination, decodes to
# https://www.pcmag.com/picks/the-best-laptops -- byte-identical to
# `_CLASSIC_ALGO_BLOCKS[0]`'s destination.
BING_COPILOT_PROMOTED_RESULT = (
    '<div class="b_wpt_bl colSpan6 rowSpan2 b_wpt_bl_h b_wpt_bl_bord" data-appns="SERP" data-k="5427">'
    '<div class="b_tpcn"><a class="tilk" aria-label="PCMag" redirecturl="" tabindex="-1" target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=6fe6a186a42db487461067486a5359f93df4722a4da18125117633e5470929abJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cucGNtYWcuY29tL3BpY2tzL3RoZS1iZXN0LWxhcHRvcHM&amp;ntb=1" h="ID=SERP,5213.1">'
    '<div class="tptxt"><div class="tptt">PCMag</div></div></a></div>'
    '<h2 class=""><a target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=6fe6a186a42db487461067486a5359f93df4722a4da18125117633e5470929abJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cucGNtYWcuY29tL3BpY2tzL3RoZS1iZXN0LWxhcHRvcHM&amp;ntb=1" '
    'h="ID=SERP,5213.2">The Best Laptops We\'ve Tested (August 2026) | PCMag</a></h2>'
    '<div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">'
    '<span class="news_dt">1. Aug. 2026</span> · Whether you want a simple budget PC, a productivity workhorse, or a screamer of a gaming notebook, our experts …'
    '</p></div>'
    '</div>'
)

# The promoted result's three siblings in the SAME answer widget -- real
# content, trimmed of embedded base64 image data and CSS/tracking bloat
# (each real block was 3-11 KB; trimming keeps the one property under test:
# each carries a real `u=a1`- or `aclk`-shaped href SOMEWHERE, and NONE of
# them has that href, or any anchor at all, inside an `<h2>`). This is what
# proves the fix discriminates on the h2-scoped anchor rather than "does
# this div merely contain an organic-looking link anywhere."
BING_COPILOT_WPT_YOUTUBE = (
    '<div class="b_wpt_bl colSpan3 rowSpan2 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="5164">'
    '<div class="mgzsvl3x2 mgzv"><div id="mc_vtvc_SERP_3" class="mc_vtvc b_canvas emb mc_vtvc_cc">'
    '<a aria-label="Top 3 Best Laptops of 2026 von YouTube" data-dc="vtdc_default" class="mc_vtvc_link" target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=d6b6728938ed283b937799145129330708b1a935d8f6775a931780a75081b6e4JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1L3ZpZGVvcy9yaXZlcnZpZXcvcmVsYXRlZHZpZGVvP3E9YmVzdCtsYXB0b3ArMjAyNiYmbWlkPTc1OTgwNTAxOTdDQjlDMDVCQjlCNzU5ODA1MDE5N0NCOUMwNUJCOUImY2h1cmw9aHR0cHMlM2ElMmYlMmZ3d3cueW91dHViZS5jb20lMmZjaGFubmVsJTJmVUN5Ny1sUDZJSDg3bHp4NWVhTVVEWXFRJkZPUk09VkFNR1pD&amp;ntb=1" h="ID=SERP,5614.1">'
    '<div class="mc_vtvc_con_rc"><div class="mc_vtvc_tl">Top 3 Best Laptops of 2026</div></div>'
    '</a></div></div></div>'
)
BING_COPILOT_WPT_TOPIC_STRIP = (
    '<div class="b_wpt_bl colSpan3 rowSpan2 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="5160">'
    '<div class="b_tophbb bgtopwh"><div class="magfilter-list-container no-image gsrow-2 gscol-2">'
    '<a target="_blank" class="pcc-lnk magtitle-link" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=f00abfecd75a26ff925c2ec6cde8578b6960a445367f7e0c966d7725f468cdd5JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1L3Nob3AvdG9waWNzP3E9YmVzdCtsYXB0b3ArMjAyNg&amp;ntb=1" h="ID=SERP,5618.1">'
    '<div class="magfilter-list-title" role="heading" aria-level="2">Mehr entdecken</div></a>'
    '<nav class="magfilter-list-nav" aria-label="Mehr entdecken"><div class="magfilter-list-ul">'
    '<a class="pcc-lnk" target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=246c9752a6a04210d90765bf20b80d046bcf1a726a2aedbb9f9d2b3d01549b8dJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1L3Nob3AvdG9waWNzP3E9TGFwdG9wIFRlc3QgMjAyNCZGT1JNPVNBTUdGTCZvcmlnaW5JR1VJRD00RTE1QzFBNDRENDI0NjYxOENEMzlDREY4QkM1MDU1Mg&amp;ntb=1" h="ID=SERP,5631.1">'
    '<div class="magfilter-title" title="Laptop Test 2024"><div class="magfilter-title-txt">Laptop Test 2024</div></div></a>'
    '</div></nav></div></div></div>'
)
BING_COPILOT_WPT_AD_CARD = (
    '<div class="b_wpt_bl colSpan6 rowSpan4 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="5121">'
    '<div class="b_ad b_adcard top_ads_magazine"><div class="acf_mma_fivebyfour"><div class="mma_acf_ad ad_sc" ce="adembmma">'
    '<div class="mma_acf_label_container"><span class="mma_badge_slug"><span>Gesponsert</span></span></div>'
    '<div class="mma_acf_textinfo">'
    '<a id="DVUWatu6es" class="" target="_blank" role="link" tabindex="0" '
    'href="https://www.bing.com/aclk?ld=e82vE_Oab7m46wwsPki5iqFzVUCUwql0KGfwpnBQwKG8sUzPIML-UPjhicnxXPLk0eCVIwbPmlYiaheXzt2ibuW56tpHsV83PUD61MSaQq3M_yappIuAjXfsmQ73AiU13cO2B1gsjEuImaKFSWH_r5_vpNP2eiPG1PfV-FxkKx6DouWrWIR-GxuylU8_TGUIyfDCBQKoFGwcqGWz5v3IMJjVx7hN4&amp;u=aHR0cHMlM2ElMmYlMmZ3d3cudmVyZ2xlaWNoLm9yZyUyZmxhcHRvcCUyZiUzZnV0bV9zb3VyY2UlM2RiaW5nJTI2dXRtX21lZGl1bSUzZGNwYyUyNnV0bV9jb250ZW50JTNkc2VhcmNoJTI2dXRtX3Rlcm0lM2RhaWQtMTQ2MDAwOTU4LWMtMjc3ODMyMzIwJTI2bXNjbGtpZCUzZDQ3NDhkOWRlMjIyZjE4ZDBjNzk3YjM0ZjY4ZDZlYWRl&amp;rlid=4748d9de222f18d0c797b34f68d6eade&amp;ntb=1" h="ID=SERP,5701.1,Ads">'
    '<div class="mma_acf_title">Laptop Test 2026</div></a>'
    '</div></div></div></div></div>'
)

# SYNTHETIC, not cut from any capture: none of the three real siblings above
# happens to render a `.b_caption`, so nothing above would catch a regression
# that widened `snippets`/`result_blocks` to any `div.b_wpt_bl` regardless of
# an h2-scoped anchor (the mirror image of the video/topic-strip case, which
# covers `titles`/`links`). This card is a plausible near-future shape --
# Bing already reuses the exact "favicon + caption" card template for the
# organic promotion, so an ad or teaser reusing the SAME caption markup
# without an h2 is not a stretch -- included specifically so that gap has a
# fixture instead of being silently unguarded.
BING_COPILOT_WPT_HYPOTHETICAL_CAPTION_ONLY = (
    '<div class="b_wpt_bl colSpan3 rowSpan2 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="9001">'
    '<div class="b_tpcn"><a class="tilk" target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=synthetic&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmFkcy8&amp;ntb=1">'
    '<div class="tptxt"><div class="tptt">example-ads.test</div></div></a></div>'
    '<div class="b_caption"><p class="b_lineclamp2">Synthetic ad copy that must never surface as a snippet.</p></div>'
    '</div>'
)

# SYNTHETIC, not cut from any capture: the property `u=a1` is load-bearing
# for -- both descriptions name it as the reason titles/links key on the
# anchor rather than the bare container -- has no fixture that actually
# needs it. Every real sibling above lacking an h2 is already excluded by
# the h2 gate alone; nothing forces `u=a1` itself to do any work. This card
# is exactly the shape both descriptions warn about: a future ad teaser
# that grows an `<h2>` (Bing already puts a `Gesponsert` badge next to an
# `h2`-styled title on other ad templates -- see BING_COPILOT_AD_BLOCK,
# whose ads use `h2` too, just outside `div.b_wpt_bl`). Its href is a real
# `aclk` ad redirect with no `u=a1` anywhere -- if the `u=a1` filter were
# ever dropped and only the h2-inside-b_wpt_bl gate kept, this ad's title
# would surface as organic (with `links` still null, since the post_process
# regex is a second, independent check -- a half-corrupted row: a believable
# title with no way to tell it apart from a real one, paired with a null
# link that alignment can't flag because every column stays the same length).
BING_COPILOT_WPT_AD_WITH_H2 = (
    '<div class="b_wpt_bl colSpan3 rowSpan2 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="9002">'
    '<div class="mma_acf_label_container"><span class="mma_badge_slug"><span>Gesponsert</span></span></div>'
    '<h2 class=""><a target="_blank" '
    'href="https://www.bing.com/aclk?ld=synthetic&amp;u=aHR0cHM6Ly9leGFtcGxlLmFkcy8&amp;ntb=1">'
    'Laptop Test 2026 - Gesponserter Vergleich</a></h2>'
    '</div>'
)

# SYNTHETIC, not cut from any capture: pins the `[?&]` boundary in front of
# `u=a1` (both the selector's `href*='&u=a1'`/`href*='?u=a1'` alternatives
# and the links post_process regex `[?&]u=a1(...)` share this same
# boundary). `&featru=a1zzzz` contains the bare substring `u=a1` -- a
# selector written as `href*='u=a1'` with no boundary character would match
# it -- but not `&u=a1` or `?u=a1`, since the character immediately before
# `u=a1` is `r`, not `&` or `?`. A believable, unrelated tracking parameter
# is exactly the shape that would produce this coincidentally: nothing about
# Bing's own tracking params guarantees `u=a1` never appears as a tail
# fragment of a longer parameter name.
BING_COPILOT_WPT_BOUNDARY_DECOY = (
    '<div class="b_wpt_bl colSpan3 rowSpan2 b_wpt_bl_h b_wpt_bl_back" data-appns="SERP" data-k="9003">'
    '<h2 class=""><a target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;featru=a1zzzz&amp;ntb=1">'
    'Boundary decoy title that must never surface</a></h2>'
    '</div>'
)

# SYNTHETIC, not cut from any capture: pins the CONTAINER half of the gate,
# independent of the anchor half the two fixtures above pin. This anchor's
# href has the correct `&u=a1` shape (boundary included) and sits inside an
# `<h2>`, exactly like a real organic anchor -- the only thing wrong with it
# is its container's class, `unrelated_widget`, is neither `b_algo` nor
# `b_wpt_bl`. A selector that dropped the container-class requirement (e.g.
# widened from `li.b_algo h2 a[...], div.b_wpt_bl h2 a[...]` to a bare
# `h2 a[...]` with no container qualifier at all) would accept it.
BING_COPILOT_UNRELATED_CONTAINER_DECOY = (
    '<div class="unrelated_widget">'
    '<h2 class=""><a target="_blank" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;u=a1zzzz&amp;ntb=1">'
    'Unrelated container decoy title that must never surface</a></h2>'
    '</div>'
)

# The AI-answer prose block. Its three <h2>s (verbatim text from the
# capture) are genuinely anchor-free -- checked directly against the real
# element, 0 of 0 -- but the ELEMENT overall is not link-free: it holds 22
# real anchors decoding to 5 distinct external hosts. Two representative
# ones (`a.md_citlink`, real text, real `u=a1` hrefs to forbes.com and
# pcwelt.de) are included here, verbatim, as siblings OUTSIDE the <h2>s --
# reproducing the actual shape (real off-host links present, just never
# inside an <h2>) instead of the easier-to-write-but-wrong "no links at
# all" shape an earlier draft of this fixture used.
BING_COPILOT_AI_ANSWER = (
    '<li class="b_ans b_top" data-tag="" data-partnertag="" data-id="" '
    'data-fbhlsel="li.b_ans.b_top" h="SERP,5549.1">'
    '<h2 class="">The top laptops of 2026 include the Apple MacBook Air M4 '
    'for overall performance, Acer Swift 16 AI SF16-71T for high-end '
    'productivity, and Alienware 16X Aurora for gaming.</h2>'
    '<h2 class="">Top Recommendations by Category</h2>'
    '<h2 class="">Key Considerations for 2026 Laptops</h2>'
    '<div class="answer_container">'
    '<a src="" class="md_citlink" target="_blank" aria-label="Quellen: Forbes" data-sups="1" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=e435cb28b8ceaf1bde4fc81a261bad94556949eb5dda63b834debaab83ca28f1JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cuZm9yYmVzLmNvbS9zaXRlcy9mb3JiZXMtcGVyc29uYWwtc2hvcHBlci9hcnRpY2xlL2Jlc3QtbGFwdG9wLw&amp;ntb=1">Forbes</a>'
    '<a src="" class="md_citlink" target="_blank" aria-label="Quellen: www.pcwelt.de" data-sups="2" '
    'href="https://www.bing.com/ck/a?!&amp;&amp;p=a64d8039c870dc7a78ada9597eaeb43e640bb6535981cf910aa49dce0e933413JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cucGN3ZWx0LmRlL2FydGljbGUvMjIxNTM4NS9kaWUtYmVzdGVuLWxhcHRvcHMtdGVzdC5odG1s&amp;ntb=1">www.pcwelt.de</a>'
    '</div>'
    '</li>'
)

# The real ad block: outer `<li class="b_ad b_adBottom">` wrapping three
# individually-tracked ads as nested `<li>`s -- two with class="" (an EMPTY
# attribute, not an absent one) and one `b_adLastChild`. Real hrefs, real
# "Gesponsert" badges, real ad copy, verbatim from the capture.
BING_COPILOT_AD_BLOCK = (
    '<li class="b_ad b_adBottom" data-partnertag="" data-id="">'
    '<li class="">'
    '<div class="b_attribution"><div class="b_adurl"><cite>https://www.welt.de › Test+Vergleich › 2026</cite></div></div>'
    '<h2 class=""><a target="_blank" href="https://www.bing.com/aclk?ld=e8ZTPKPVka8iAXSgniYLMK8TVUCUz-n3RDVbSqsxqkZk-aQEEkbuk7E_Hjte2zBJ4dbetnUMkfqKyUufn5udtl44dvEYuGxAaRq7_wBbyaCxfE99K2qn9UU7x_3O3aP05k8xsL200f-sodcc1MAwwbeOOkfb_l4bsG62aG9TKBsGa33Uj15WS9pjhJfmbg7ZPAfozfZe06AeL1HgNVYsAeT4Ob8S0&amp;u=aHR0cHMlM2ElMmYlMmZ3d3cud2VsdC5kZSUyZnZlcmdsZWljaCUyZmxhcHRvcCUyZiUzZnV0bV9zb3VyY2UlM2RiaW5nJTI2dXRtX21lZGl1bSUzZGNwYyUyNnV0bV9jb250ZW50JTNkc2VhcmNoJTI2dXRtX3Rlcm0lM2RhaWQtMTQ2MDEyNDg4LWMtNjUwNDI2NjEzJTI2bXNjbGtpZCUzZDc3Zjg2ZGZhMDQxNzEzYWNjZjlhMDc2YjRlZWU3NmMx&amp;rlid=77f86dfa041713accf9a076b4eee76c1&amp;ntb=1" h="ID=SERP,5652.1,Ads">Studenten-Laptop Test 2026 - WELT Test &amp; Vergleich 2026</a></h2>'
    '<div class="b_caption"><div class="b_ad_description"><span class="ta-slug-pos-wrapper"><span>Gesponsert</span></span>Alle Produkte im Vergleich 2026. Sieger online finden und fundierte Entscheidung treffen! Aktuelle Top 7 gesucht? Jetzt Studenten-Laptop vergleichen &amp; günstig bestellen!</div></div>'
    '</li>'
    '<li class="">'
    '<div class="b_attribution"><div class="b_adurl"><cite>https://warentest.vergleich.org › Test+Vergleich › Notebook</cite></div></div>'
    '<h2 class=""><a target="_blank" href="https://www.bing.com/aclk?ld=e876cN8ztSRXcfJSHd2ov6ozVUCUxG4u-AhGTHMEvZfHaA3xcxSJs4tuJ63JA5vlM-AimmKL25epS9BsQ_C-AP3JUFVMHcR0jgfQjDp7WNH-Ha0_JCABCeh4Zy3nuo8MUH6lZzjJV4xtFikqnv98RFDCDh-UpdJ9Uqn9MqmLHkgk96pMd-sFSNy9NLX3MfSjIf2LXSTMQZIbRB0VP94yPlC9YtDXY&amp;u=aHR0cHMlM2ElMmYlMmZ3YXJlbnRlc3QudmVyZ2xlaWNoLm9yZyUyZmxhcHRvcCUyZiUzZnV0bV9zb3VyY2UlM2RiaW5nJTI2dXRtX21lZGl1bSUzZGNwYyUyNnV0bV9jb250ZW50JTNkc2VhcmNoJTI2dXRtX3Rlcm0lM2RhaWQtMTQ2MDAwOTU4LWMtMjc3ODMyMzIwJTI2bXNjbGtpZCUzZGI4ZGQ0YmFjNThmNTExM2MwMzU5MjEzZDEyMGM0ZGMz&amp;rlid=b8dd4bac58f5113c0359213d120c4dc3&amp;ntb=1" h="ID=SERP,5676.1,Ads">Notebook sehr gut - Test 2026 - Die besten Produkte</a></h2>'
    '<div class="b_caption"><div class="b_ad_description"><span class="ta-slug-pos-wrapper"><span>Gesponsert</span></span>Jetzt "Notebook Preis" vergleichen &amp; günstig online bestellen! Unsere Test- und Vergleichsverfahren sind unabhängig, objektiv und aktuell.</div></div>'
    '</li>'
    '<li class="b_adLastChild">'
    '<div class="b_attribution"><div class="b_adurl"><cite>https://www.lenovo.com › Lenovo › IdeaPad</cite></div></div>'
    '<h2 class=""><a target="_blank" href="https://www.bing.com/aclk?ld=e8KZfBr3savJPf1HoJ9YVUwTVUCUxxKuB_LnGubXyq4xGxLFrXL8GrLZFuGzF7UF2I7KyM5Qjt7Y0BzFM2Nxxor_LR6if1Bwq9M9GB5MuRyZxuUKI00TKfsWkMxZrRUaSLtlzmR_Qi-ew6kArqLgarIE_Q83qFTSitn-12S46jTCpxaZtLf2MbuIMWsHVPgcX6P30LpF1wgk2XxwlDGNTZWSJhSqk&amp;u=aHR0cHMlM2ElMmYlMmZhZC5kb3VibGVjbGljay5uZXQlMmZzZWFyY2hhZHMlMmZsaW5rJTJmY2xpY2slM2ZsaWQlM2Q0MzcwMDA3ODI3Mjk4Mjg2MyUyNmRzX3Nfa3dnaWQlM2Q1ODcwMDAwODU3NTg5OTU0MyUyNmRzX2FfY2lkJTNkMTE5NTU3NzI0MiUyNmRzX2FfY2FpZCUzZDIwNjUwNDQzMjUxJTI2ZHNfYV9hZ2lkJTNkMTU1NTc5MzY5OTM4JTI2ZHNfYV9saWQlM2Rrd2QtNjU5NDM2NjA2MSUyNiUyNmRzX2VfYWRpZCUzZDczNjY3NjM0MDA1NjQwJTI2ZHNfZV90YXJnZXRfaWQlM2Rrd2QtNzM2Njc2NDI0MDAyOTYlM2Fsb2MtNzIlMjYlMjZkc19lX25ldHdvcmslM2RvJTI2ZHNfdXJsX3YlM2QyJTI2ZHNfZGVzdF91cmwlM2RodHRwcyUzYSUyZiUyZnd3dy5sZW5vdm8uY29tJTJmZGUlMmZkZSUyZmQlMmZzYWxlJTJmbGFwdG9wcyUyZiUzZnZpc2libGVEYXRhcyUzZDM2NzIlMjUzQUlkZWFQYWQlMjUzQjM2NzAlMjUzQUFsbGUlMjUyNTIwSW50ZWwlMjUyNUMyJTI1MjVBRSUyNTI1MjBQcm96ZXNzb3JlbiUyNmNpZCUzZGRlJTNhc2VtJTNhMXdsdjlhJTI2Z2NsaWQlM2Q0MmFiNWIzMjkwY2UxNzk0ZmQyODMxYTNkYTM3ODEwZCUyNmdjbHNyYyUzZDNwLmRzJTI2JTI2bXNjbGtpZCUzZDQyYWI1YjMyOTBjZTE3OTRmZDI4MzFhM2RhMzc4MTBkJTI2dXRtX3NvdXJjZSUzZGJpbmclMjZ1dG1fbWVkaXVtJTNkY3BjJTI2dXRtX2NhbXBhaWduJTNkQi1ERS1TRU0tQ09OU1VNRVItUFVCTElDLSUyNTIwU2VhcmNoJTI2dXRtX3Rlcm0lM2RsYXB0b3AlMjUyMGlkZWFwYWQlMjZ1dG1fY29udGVudCUzZElkZWFQYWQlMjUyMC0lMjUyMEludGVs&amp;rlid=42ab5b3290ce1794fd2831a3da37810d&amp;ntb=1" h="ID=SERP,5906.1,Ads">Lenovo™ Online-Shop - Lenovo™ IdeaPad Ultrabooks</a></h2>'
    '<div class="b_caption"><div class="b_ad_description"><span class="ta-slug-pos-wrapper"><span>Gesponsert</span></span>Lenovo IdeaPad mit Intel® Core™. Vielseitige Laptops für jeden Geschmack. Jetzt bestellen! NEU: Jetzt mit 0% Finanzierung ab 800€ Einkaufswert mit paypal</div></div>'
    '</li>'
    '</li>'
)

# The real "Benutzer suchen auch nach" / related-searches heading. Its own
# `<h2>` has NO enclosing anchor at all (not even an empty one) -- it is a
# section heading, not a result. The suggestion chips it introduces (7 plain
# query-refinement links, none inside an `<h2>`) are dropped: selector-
# irrelevant either way, since nothing here targets a bare `<a>`.
BING_COPILOT_RELATED_SEARCHES = (
    '<li class="b_ans" data-tag="" data-partnertag="" data-id="" h="SERP,5836.1">'
    '<div class="b_rs rsExplr" id="brsv3">'
    '<h2 class="">Ausführliche Informationen zu <strong>best laptop 2026</strong></h2>'
    '</div></li>'
)

BING_COPILOT_ALGO_BLOCKS = [
    '<li class="b_algo" data-id="" iid="SERP.5833"><div class="b_tpcn"><a class="tilk" aria-label="CHIP" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=10b2f26e35f5f95b43cf32d4d0810e43cb90dcd5f91861f5e28f6185f85c497bJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1" h="ID=SERP,5241.1"><div class="tptxt"><div class="tptt">CHIP</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=10b2f26e35f5f95b43cf32d4d0810e43cb90dcd5f91861f5e28f6185f85c497bJmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cuY2hpcC5kZS9hcnRpa2VsL0xhcHRvcC1WZXJnbGVpY2gtRGFzLXNpbmQtZGllLU5vdGVib29rLVRlc3RzaWVnZXJfMTE5OTQxNDc1Lmh0bWw&amp;ntb=1" h="ID=SERP,5241.2">Notebook-Test.2026: Allround-Laptops auf dem Prüfstand - CHIP</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid=""><span class="news_dt">19. Juli 2026</span> · Der Markt ist groß und unübersichtlich – mit unserem Notebook Test behalten Sie jederzeit den Überblick. Wir stellen …</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5834"><div class="b_tpcn"><a class="tilk" aria-label="PC-WELT" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=a64d8039c870dc7a78ada9597eaeb43e640bb6535981cf910aa49dce0e933413JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cucGN3ZWx0LmRlL2FydGljbGUvMjIxNTM4NS9kaWUtYmVzdGVuLWxhcHRvcHMtdGVzdC5odG1s&amp;ntb=1" h="ID=SERP,5263.1"><div class="tptxt"><div class="tptt">PC-WELT</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=a64d8039c870dc7a78ada9597eaeb43e640bb6535981cf910aa49dce0e933413JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cucGN3ZWx0LmRlL2FydGljbGUvMjIxNTM4NS9kaWUtYmVzdGVuLWxhcHRvcHMtdGVzdC5odG1s&amp;ntb=1" h="ID=SERP,5263.2">Laptop-Test 2026: Die besten Notebooks aller Klassen im Vergleich</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">Unser Notebook-Testvergleich 2026 bietet Ihnen einen kompakten Überblick und hilft, den besten Laptop für Ihre Bedürfnisse zu finden.</p></div></li>',
    '<li class="b_algo" data-id="" iid="SERP.5835"><div class="b_tpcn"><a class="tilk" aria-label="Tom\'s Hardware" redirecturl="" tabindex="-1" target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=85577a8daa51a6c0266d803f9a1b9455c3bd24a9d6df34eceeb4b99500dc8874JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cudG9tc2hhcmR3YXJlLmNvbS9sYXB0b3BzL2Jlc3QtbGFwdG9wcw&amp;ntb=1" h="ID=SERP,5283.1"><div class="tptxt"><div class="tptt">Tom\'s Hardware</div></div></a></div><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=85577a8daa51a6c0266d803f9a1b9455c3bd24a9d6df34eceeb4b99500dc8874JmltdHM9MTc4Nzg3NTIwMA&amp;ptn=3&amp;ver=2&amp;hsh=4&amp;fclid=027e2045-f835-6949-0d7c-3786f9986817&amp;u=a1aHR0cHM6Ly93d3cudG9tc2hhcmR3YXJlLmNvbS9sYXB0b3BzL2Jlc3QtbGFwdG9wcw&amp;ntb=1" h="ID=SERP,5283.2">Best Laptops 2026: Our benchmarked picks for productivity, portability ...</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">The MacBook Air has been a go-to laptop recommendation for quite some time, thanks to strong performance, a fanless design, …</p></div></li>',
]

# Assembled in the real page's document order: the answer-widget area
# (`ol#b_topw` in the real page -- promoted result + its non-result
# siblings) renders BEFORE `#b_results`, which itself opens with the AI
# prose, then the three organic b_algo cards, then the ad block, then the
# related-searches heading.
BING_COPILOT_SERP = (
    "<html><body>"
    "<div id=\"b_topw_area\">"
    + BING_COPILOT_PROMOTED_RESULT
    + BING_COPILOT_WPT_YOUTUBE
    + BING_COPILOT_WPT_TOPIC_STRIP
    + BING_COPILOT_WPT_AD_CARD
    + BING_COPILOT_WPT_HYPOTHETICAL_CAPTION_ONLY
    + BING_COPILOT_WPT_AD_WITH_H2
    + BING_COPILOT_WPT_BOUNDARY_DECOY
    + BING_COPILOT_UNRELATED_CONTAINER_DECOY
    + "</div>"
    "<ol id=\"b_results\">"
    + BING_COPILOT_AI_ANSWER
    + "".join(BING_COPILOT_ALGO_BLOCKS)
    + BING_COPILOT_AD_BLOCK
    + BING_COPILOT_RELATED_SEARCHES
    + "</ol></body></html>"
)


class TestBingSearchClassicLayout:
    def setup_method(self):
        self.preset = _load("bing_search_chromium")

    def test_ten_aligned_organic_rows_unaffected_by_the_widened_selector(self):
        """The classic capture has zero `div.b_wpt_bl` anywhere, so widening
        titles/links/snippets/result_blocks to also match that container
        must be a complete no-op here."""
        data, warnings = extract_fields(BING_CLASSIC_SERP, self.preset.parsing_instructions)
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == 10, f"{key}: {data[key]!r}"
        assert not warnings
        assert all(title for title in data["titles"])
        assert data["titles"][0] == "The Best Laptops We've Tested (August 2026) | PCMag"

    def test_links_decoded_off_bing_host(self):
        data, _ = extract_fields(BING_CLASSIC_SERP, self.preset.parsing_instructions)
        assert data["links"][0] == "https://www.pcmag.com/picks/the-best-laptops"
        assert data["links"][1] == (
            "https://www.chip.de/artikel/Laptop-Vergleich-Das-sind-die-"
            "Notebook-Testsieger_119941475.html"
        )
        for link in data["links"]:
            assert link.startswith("https://")
            assert "bing.com" not in link


class TestBingSearchRichCard:
    """bing_search_chromium/de, 2026-09-04 audit and 2026-09-05 probe: 3 titles,
    3 links, 2 snippets -- the rich-card result has no `<p>`."""

    def setup_method(self):
        self.preset = _load("bing_search_chromium")

    def test_a_rich_card_keeps_its_row_with_a_null_snippet(self):
        data, warnings = extract_fields(BING_RICH_CARD_SERP, self.preset.parsing_instructions)
        assert len(data["titles"]) == len(data["links"]) == len(data["result_blocks"]) == 3
        assert data["titles"][1] == "Notebook-Test.2026: Allround-Laptops auf dem Prüfstand - CHIP"
        assert len(data["snippets"]) == 3, data["snippets"]
        assert data["snippets"][1] is None
        assert data["snippets"][0].startswith("1. Aug. 2026 · Whether you want")
        assert data["snippets"][2].startswith("Unser Notebook-Testvergleich 2026")
        assert not [w for w in warnings if "row_alignment" in w], warnings

    def test_a_sitelinks_description_is_not_promoted_to_the_snippet(self):
        serp = "<html><body><ol id=\"b_results\">" + _CLASSIC_ALGO_BLOCKS[0] + BING_DEEP_LINKS_ALGO + "</ol></body></html>"
        data, warnings = extract_fields(serp, self.preset.parsing_instructions)
        assert len(data["titles"]) == len(data["links"]) == len(data["snippets"]) == 2
        assert data["snippets"][1] is None, data["snippets"]
        assert not [w for w in warnings if "row_alignment" in w], warnings


class TestBingSearchCopilotLayout:
    """The layout that under-collected live: 2 rows and 5 rows measured
    2026-08-27, both times with row_alignment_mismatch. Ground truth on
    THIS layout is FOUR organic rows: the three `li.b_algo` cards inside
    `#b_results` PLUS the rank-1 result Bing promotes into the answer
    widget as `div.b_wpt_bl` -- not three, which is what a first pass at
    this fix (which never looked outside `#b_results`) returned.
    """

    def setup_method(self):
        self.preset = _load("bing_search_chromium")

    def test_four_aligned_organic_rows_promoted_result_first(self):
        data, warnings = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert data["titles"] == [
            "The Best Laptops We've Tested (August 2026) | PCMag",
            "Notebook-Test.2026: Allround-Laptops auf dem Prüfstand - CHIP",
            "Laptop-Test 2026: Die besten Notebooks aller Klassen im Vergleich",
            "Best Laptops 2026: Our benchmarked picks for productivity, portability ...",
        ]
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == 4, f"{key}: {data[key]!r}"
        assert not warnings

    def test_promoted_result_decodes_to_the_same_destination_as_classic_rank_one(self):
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        classic_data, _ = extract_fields(BING_CLASSIC_SERP, self.preset.parsing_instructions)
        assert data["links"][0] == classic_data["links"][0] == "https://www.pcmag.com/picks/the-best-laptops"
        assert data["titles"][0] == classic_data["titles"][0]

    def test_links_decode_and_stay_off_bing_host(self):
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert data["links"] == [
            "https://www.pcmag.com/picks/the-best-laptops",
            "https://www.chip.de/artikel/Laptop-Vergleich-Das-sind-die-"
            "Notebook-Testsieger_119941475.html",
            "https://www.pcwelt.de/article/2215385/die-besten-laptops-test.html",
            "https://www.tomshardware.com/laptops/best-laptops",
        ]
        for link in data["links"]:
            assert "bing.com" not in link

    def test_ai_generated_answer_prose_excluded_even_though_it_carries_real_links(self):
        """The b_top element's OWN <h2>s have zero anchors and no
        destination of their own -- but the element as a whole is NOT
        link-free (it carries real citation footnotes to forbes.com and
        pcwelt.de, included in this fixture). Neither the generated prose
        nor those citation footnotes may leak into titles/links: the prose
        because it names no h2>a at all, the footnotes because they live
        outside every h2 even though their hrefs are real and off-host."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        joined_titles = " | ".join(data["titles"])
        assert "top laptops of 2026 include" not in joined_titles
        assert "Top Recommendations by Category" not in data["titles"]
        assert "Key Considerations for 2026 Laptops" not in data["titles"]
        assert "Forbes" not in data["titles"]
        haystack = json.dumps(data, ensure_ascii=False)
        # the citation anchors are real and off-host, so only their SPECIFIC
        # destinations (not a generic "bing.com" check) prove they didn't
        # leak in through some other field.
        assert "forbes.com" not in haystack
        assert "pcwelt.de/article/2215385" not in haystack or haystack.count("pcwelt.de/article/2215385") == 1

    def test_wpt_siblings_excluded_despite_carrying_organic_shaped_hrefs(self):
        """The three div.b_wpt_bl siblings each carry a real, resolvable
        `u=a1`/`aclk` href, but none inside an `<h2>`. This is the exact
        false-positive an early attempt at this fix produced by keying on
        "does this div contain a u=a1 link anywhere" instead of "does this
        div have an h2>a carrying one": that check pulled in the YouTube
        card, the topic-filter strip AND the ad card."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Top 3 Best Laptops of 2026" not in haystack
        assert "Mehr entdecken" not in haystack
        assert "Laptop Test 2024" not in haystack
        assert "/shop/topics" not in haystack
        assert "/videos/riverview" not in haystack

    def test_ad_with_h2_still_excluded_by_the_u_a1_requirement(self):
        """The `u=a1` requirement is load-bearing, not redundant with the h2
        gate: this ad card has a real Gesponsert badge AND its title anchor
        sits inside an `<h2>`, so the h2 gate alone would accept it. Its
        href (`aclk`, no `u=a1` anywhere) is what the discriminator actually
        excludes it on. If `u=a1` were ever dropped from the wpt half of the
        selector (keeping only "inside an h2 inside a div.b_wpt_bl"), this
        title would surface as organic with a null link riding along --
        every column would still be the same length, so row_alignment_
        mismatch could not catch it, and `required` on titles only checks
        for zero matches, not a wrong one slipping in among real ones."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert "Laptop Test 2026 - Gesponserter Vergleich" not in data["titles"]
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Gesponserter Vergleich" not in haystack

    def test_boundary_decoy_still_excluded_by_the_ampersand_question_mark_gate(self):
        """Pins the `[?&]` boundary character in front of `u=a1`: this
        decoy's href contains the bare substring `u=a1` (via
        `...&featru=a1zzzz...`) but not `&u=a1` or `?u=a1` -- the character
        immediately before `u=a1` is `r`, not a parameter separator. A
        selector written as `href*='u=a1'` with no boundary check would
        match it; the shipped `href*='&u=a1'`/`href*='?u=a1'` pair must
        not."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert "Boundary decoy title that must never surface" not in data["titles"]
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Boundary decoy" not in haystack

    def test_unrelated_container_still_excluded_regardless_of_a_correct_anchor(self):
        """Pins the CONTAINER half of the gate: this anchor has the correct
        `&u=a1` shape and sits inside an `<h2>`, same as a real organic
        anchor -- only its container's class (`unrelated_widget`) is neither
        `b_algo` nor `b_wpt_bl`. A selector that dropped the container-class
        qualifier (matching a bare `h2 a[href*='&u=a1']` anywhere in the
        document) would accept it."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert "Unrelated container decoy title that must never surface" not in data["titles"]
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Unrelated container decoy" not in haystack

    def test_synthetic_caption_bearing_sibling_without_h2_excluded(self):
        """Mirror image of the h2-carrying siblings above, covering
        `snippets`/`result_blocks` instead of `titles`/`links`: a container
        that has a `.b_caption` but no `<h2>` must not surface a snippet.
        Not observed on the real capture (none of the three real siblings
        happens to render a `.b_caption`) -- included so a regression that
        drops the h2 gate specifically on the snippets/result_blocks side
        has a fixture that catches it too."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Synthetic ad copy" not in haystack
        assert "example-ads.test" not in haystack

    def test_ads_excluded_from_every_field(self):
        """The nested ad entries (2 empty-class + 1 b_adLastChild <li>, all
        inside <li class="b_ad b_adBottom">) and the wpt-area ad card carry a
        visible "Gesponsert" badge and an `aclk` (not `ck/a`) redirect. None
        of that may reach titles/links/snippets."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Gesponsert" not in haystack
        # "WELT" alone would false-positive on the genuine "PC-WELT" organic
        # result a few rows over; the ad's own title text is unambiguous.
        assert "Studenten-Laptop" not in haystack
        assert "Vergleich.org" not in haystack
        assert "Lenovo" not in haystack
        assert "aclk" not in haystack
        assert "Laptop Test 2026" not in data["titles"]  # the ad card's own title

    def test_related_searches_heading_excluded(self):
        """The heading has no anchor at all; it must not surface as a title
        with a null link riding alongside it."""
        data, _ = extract_fields(BING_COPILOT_SERP, self.preset.parsing_instructions)
        assert "Ausführliche Informationen" not in " | ".join(data["titles"])


class TestBingSearchLinksPostProcessPinned:
    """Pins the property the descriptions and the ad-exclusion reasoning
    both lean on: the organic redirect is `u=a1<base64>`, not merely
    `u=<base64>` (Bing's own ad redirect uses the latter). A loosened
    regex like `u=(?:a1)?` would still pass every fixture above by
    coincidence (every ad href in these fixtures is excluded structurally,
    by container/h2 scoping, before post_process ever runs on it) -- so
    the property needs its own direct pin, not just reliance on the
    fixtures never feeding an ad href into this pipeline.
    """

    def setup_method(self):
        self.preset = _load("bing_search_chromium")

    def test_the_pipeline_still_requires_the_a1_prefix(self):
        """The `a1` prefix moved from a regex into `unwrap_param`'s own
        argument when the pipeline had to start surviving uBO-sanitized
        hrefs, but it is the same property: Bing's ORGANIC redirect is
        `u=a1<base64>` and its AD redirect is `u=<base64>`, so dropping the
        prefix would make the two indistinguishable."""
        steps = self.preset.parsing_instructions.fields["links"].post_process
        assert [s.op for s in steps] == [
            "unwrap_param", "null_if_regex", "null_if_regex",
        ]
        assert steps[0].args == ["u", "base64url", "a1", "bing.com"]

    def test_anything_left_on_bing_com_is_nulled(self):
        """The second half of the defence the `a1` prefix used to carry alone.

        `unwrap_param` never nulls -- passing the original through is what
        lets one pipeline serve both the wrapped and the uBO-sanitized shape.
        So the "an ad must not come back as a plausible link" property is
        carried by the step after it: a value still pointing at bing.com is
        one nothing recovered a destination from, and it is nulled rather
        than shipped."""
        from src.extract.extractor import _apply_post_process

        steps = self.preset.parsing_instructions.fields["links"].post_process
        for wrapper in (
            "https://www.bing.com/ck/a?!&&p=x&featru=a1zzzz&ntb=1",
            "https://www.bing.com/ck/a?!&&p=x&u=notbase64!!&ntb=1",
            # Uppercase host: the first leak measured on 2026-09-04 defeated a
            # case-sensitive check.
            "https://WWW.BING.COM/aclk?ld=e8&u=aHR0cA&ntb=1",
            # Not an absolute destination: relative and javascript: shapes are
            # not links a caller can follow, and must not be handed on as if
            # they were.
            "/videos/search?q=laptop",
            "javascript:void(0)",
        ):
            assert _apply_post_process(wrapper, steps, "links", [], set()) is None, wrapper

    def test_a_destination_that_merely_mentions_bing_is_not_dropped(self):
        """Host, not substring. A real result whose QUERY names bing.com was
        nulled by the link guard and dropped by the anchor gate — and dropping
        it from titles/links alone SHIFTS the columns, because snippets and
        result_blocks keep the `li.b_algo` container unconditionally."""
        from src.extract.extractor import _apply_post_process

        steps = self.preset.parsing_instructions.fields["links"].post_process
        url = "https://example.org/article?source=bing.com"
        assert _apply_post_process(url, steps, "links", [], set()) == url

        html = (
            '<html><body><ol id="b_results"><li class="b_algo"><h2>'
            f'<a href="{url}">Article that cites bing.com</a></h2></li></ol></body></html>'
        )
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        assert data["titles"] == ["Article that cites bing.com"]
        assert data["links"] == [url]

    def test_a_destinations_own_u_parameter_is_not_decoded_as_bings(self):
        """`unwrap_param` reads a parameter out of a redirect. Once uBO has
        replaced the href with the DESTINATION, that query belongs to the
        destination — decoding it a second time shipped a different site than
        the page actually linked to."""
        from src.extract.extractor import _apply_post_process

        steps = self.preset.parsing_instructions.fields["links"].post_process
        url = "https://trusted.example/page?u=a1aHR0cHM6Ly9vdGhlci5leGFtcGxl"
        assert _apply_post_process(url, steps, "links", [], set()) == url

    def test_a_real_destination_survives_all_three_steps(self):
        """The guards above must not be so wide they null the answer."""
        from src.extract.extractor import _apply_post_process

        steps = self.preset.parsing_instructions.fields["links"].post_process
        assert _apply_post_process(
            "https://www.pcmag.com/picks/the-best-laptops", steps, "links", [], set()
        ) == "https://www.pcmag.com/picks/the-best-laptops"

    def test_ad_shaped_href_does_not_decode_even_if_fed_to_the_pipeline(self):
        """Behavioural pin, not just a string match: if an ad's `u=`
        (no `a1` prefix) were ever handed to this exact post_process
        pipeline -- e.g. because a future container-scoping regression let
        one through -- it must come back null, not a mis-decoded value."""
        from src.extract.extractor import _apply_post_process

        links_rule = self.preset.parsing_instructions.fields["links"]
        ad_href = (
            "https://www.bing.com/aclk?ld=e82vE...&u=aHR0cHMlM2ElMmYlMmZ3d3cudmVyZ2xlaWNoLm9yZyUyZmxhcHRvcCUyZg&ntb=1"
        )
        result = _apply_post_process(ad_href, links_rule.post_process, "links", [], set())
        assert result is None

    def test_titles_stays_required(self):
        """`required` is the ONLY failure signal on a blocked or empty page:
        `row_alignment_mismatch` cannot fire when every all=true field comes
        back length 0 (there is nothing to be unequal), so a titles selector
        that silently starts matching nothing -- Bing markup drift, a typo'd
        rewrite of this selector, a captcha page -- would ship a clean-
        looking empty result with zero warnings unless `required` stays
        True. Pinned directly rather than only exercised implicitly,
        because nothing above sends this preset a page where titles is
        legitimately empty."""
        titles_rule = self.preset.parsing_instructions.fields["titles"]
        assert titles_rule.required is True


class TestNaiveContainerOnlyWideningWouldRegress:
    """Illustrative only -- NOT the real regression guard (that is
    `TestBingSearchCopilotLayout.test_wpt_siblings_excluded_despite_
    carrying_organic_shaped_hrefs` and `.test_ads_excluded_from_every_
    field`, both of which run against the actual shipped preset). This
    class builds its own `ExtractRule` and documents, for a reader, the
    TWO wrong fixes this task's history tried and rejected:
      1. matching any `<li class="">` (an ad-teaser shape, not a missed
         organic one);
      2. matching any `div.b_wpt_bl` that merely CONTAINS a `u=a1` link
         anywhere (which also matches the YouTube card and the topic-filter
         strip, neither of which has an h2 at all).
    """

    def test_bare_empty_class_li_pulls_in_an_ad_title(self):
        naive_rule = ExtractRule(
            type="css",
            fields={
                "titles": FieldRule(selector="li.b_algo h2, li[class=''] h2", all=True),
                "snippets": FieldRule(
                    selector="li.b_algo .b_caption p, li.b_algo p.b_lineclamp2, "
                    "li[class=''] .b_ad_description",
                    all=True,
                ),
            },
        )
        data, _ = extract_fields(BING_COPILOT_SERP, naive_rule)
        assert any("WELT" in title for title in data["titles"])
        assert any("Gesponsert" in snippet for snippet in data["snippets"])

    def test_container_without_h2_gate_pulls_in_the_video_and_topic_strip(self):
        naive_rule = ExtractRule(
            type="xpath",
            fields={
                "titles": FieldRule(
                    type="xpath",
                    selector=(
                        "//li[contains(concat(' ', normalize-space(@class), ' '), ' b_algo ')]/h2"
                        " | //div[contains(concat(' ', normalize-space(@class), ' '), ' b_wpt_bl ')]"
                        "[.//a[contains(@href, 'u=a1')]]"
                    ),
                    all=True,
                ),
            },
        )
        data, _ = extract_fields(BING_COPILOT_SERP, naive_rule)
        # 3 b_algo h2's + the whole PCMag div (has its own h2 text via
        # text_content) + the video/topic-strip divs (no h2, so the whole
        # div's text_content becomes the "title") -- either way, more than
        # the correct 4, and it drags in non-result text.
        joined = " ".join(data["titles"])
        assert "Top 3 Best Laptops of 2026" in joined or "Mehr entdecken" in joined


# --- uBlock Origin's href-sanitizer -------------------------------------------
# Camoufox ships uBO on EVERY launch (`add_default_addons()` is called
# unconditionally inside camoufox's `launch_options()`), and uBO's Privacy list
# — enabled by default — carries:
#
#   bing.com##+js(href-sanitizer, a[href*="bing.com/ck/a"], ?u /^a1(.*)$/ -base64)
#
# whose scriptlet does `elem.setAttribute('href', hrefAfter)`. So on every
# Camoufox run the organic anchors reach us ALREADY DECODED, with no `u=a1` left
# to key on. Measured live 2026-09-04 on a real SERP: 10 anchors, 10 carrying
# `u=a1` with uBO excluded, 0 with uBO loaded — and `bing_search_camoufox` went
# from 0 selector misses on 09-02 to 4 of 6 on 09-03, its Chromium twin
# unaffected in the same run, because a container recreate had pulled a fresh
# uBO. Nothing in this repo pins uBO or its filter lists: it is downloaded at
# first browser LAUNCH, not at image build.
#
# The transformation below is not a hand-written guess at that output; it
# applies uBO's own rule to the same captures, so this fixture cannot drift from
# what the addon actually does without the rule itself changing.
_UBO_CK_A_HREF = re.compile(r'href="(https://www\.bing\.com/ck/a\?[^"]*)"')
_UBO_U_A1 = re.compile(r"[?&]u=a1([A-Za-z0-9_-]+)")


def _ubo_sanitize(html: str) -> str:
    """Rewrite `bing.com/ck/a` hrefs to their decoded destination, as uBO does.

    Faithful to the filter on three points that matter to the decoys:
      * it selects on `bing.com/ck/a`, so an `aclk` AD href is left alone;
      * it reads the query parameter named exactly `u`, so `&featru=a1zzzz`
        (the boundary decoy) is left alone;
      * a payload that does not decode is left alone rather than replaced.
    """
    def _rewrite(match: re.Match[str]) -> str:
        raw = unescape(match.group(1))
        payload = _UBO_U_A1.search(raw)
        if payload is None:
            return match.group(0)
        blob = payload.group(1)
        try:
            dest = base64.b64decode(
                (blob + "=" * (-len(blob) % 4)).encode(), altchars=b"-_", validate=True
            ).decode("utf-8")
        except Exception:  # noqa: BLE001 - mirrors uBO leaving a bad payload alone
            return match.group(0)
        return f'href="{dest}"'

    return _UBO_CK_A_HREF.sub(_rewrite, html)


BING_CLASSIC_SERP_UBO = _ubo_sanitize(BING_CLASSIC_SERP)
BING_COPILOT_SERP_UBO = _ubo_sanitize(BING_COPILOT_SERP)


class TestBingSearchUnderUblockOrigin:
    """The same captures as every class above, after uBO has rewritten them.

    This is the production posture for every `_camoufox` variant, not an exotic
    case: uBO cannot be opted out of per request, and it ships by default.
    """

    def setup_method(self):
        self.preset = _load("bing_search_camoufox")

    def test_the_fixture_really_is_sanitized(self):
        """Guards the guard: if the transformation silently stopped applying,
        every test below would pass against the ORIGINAL capture and prove
        nothing. Mutation-proofing the fixture, not the preset."""
        assert "u=a1" not in BING_CLASSIC_SERP_UBO
        assert "bing.com/ck/a" not in BING_CLASSIC_SERP_UBO
        assert "https://www.pcmag.com/picks/the-best-laptops" in BING_CLASSIC_SERP_UBO
        # The ad keeps its aclk wrapper: uBO's rule selects on `ck/a` only.
        assert "bing.com/aclk" in BING_COPILOT_SERP_UBO

    def test_ten_aligned_organic_rows_survive_sanitization(self):
        data, warnings = extract_fields(BING_CLASSIC_SERP_UBO, self.preset.parsing_instructions)
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == 10, f"{key}: {data[key]!r}"
        assert not warnings
        assert data["titles"][0] == "The Best Laptops We've Tested (August 2026) | PCMag"

    def test_links_are_the_destinations_sanitization_already_produced(self):
        """Same values the wrapped capture decodes to — the pipeline must reach
        the same answer whether uBO decoded the href or we did."""
        data, _ = extract_fields(BING_CLASSIC_SERP_UBO, self.preset.parsing_instructions)
        assert data["links"][0] == "https://www.pcmag.com/picks/the-best-laptops"
        assert data["links"][1] == (
            "https://www.chip.de/artikel/Laptop-Vergleich-Das-sind-die-"
            "Notebook-Testsieger_119941475.html"
        )
        for link in data["links"]:
            assert link.startswith("https://")
            assert "bing.com" not in link

    def test_promoted_copilot_result_survives_sanitization(self):
        """All FOUR columns, not just titles: snippets/result_blocks gate the
        `div.b_wpt_bl` container on it CONTAINING a qualifying anchor, so a
        widening that only touched titles/links would drop the promoted result
        from the other two and silently misalign the rows."""
        data, warnings = extract_fields(BING_COPILOT_SERP_UBO, self.preset.parsing_instructions)
        for key in ("titles", "links", "snippets", "result_blocks"):
            assert len(data[key]) == 4, f"{key}: {data[key]!r}"
        assert not warnings
        assert data["links"][0] == "https://www.pcmag.com/picks/the-best-laptops"

    def test_the_ad_with_an_h2_is_still_excluded_after_sanitization(self):
        """THE regression this widening could cause, and the reason the fix is
        not simply "drop the u=a1 predicate".

        `u=a1` was the only thing rejecting this card (see
        test_ad_with_h2_still_excluded_by_the_u_a1_requirement) — a real
        `Gesponsert` badge and a title anchor inside an `<h2>`, so the h2 gate
        alone accepts it. Sanitization removes `u=a1` from the ORGANIC anchors,
        so whatever replaces the predicate must keep rejecting this one, whose
        href is a bare `aclk` uBO does not touch."""
        data, _ = extract_fields(BING_COPILOT_SERP_UBO, self.preset.parsing_instructions)
        assert "Laptop Test 2026 - Gesponserter Vergleich" not in data["titles"]
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Gesponserter Vergleich" not in haystack
        assert "aclk" not in haystack

    def test_the_two_decoys_are_still_excluded_after_sanitization(self):
        data, _ = extract_fields(BING_COPILOT_SERP_UBO, self.preset.parsing_instructions)
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Boundary decoy" not in haystack
        assert "Unrelated container decoy" not in haystack

    def test_the_widgets_without_an_h2_anchor_are_still_excluded(self):
        data, _ = extract_fields(BING_COPILOT_SERP_UBO, self.preset.parsing_instructions)
        haystack = json.dumps(data, ensure_ascii=False)
        assert "Mehr entdecken" not in haystack
        assert "/videos/riverview" not in haystack
        assert "Top 3 Best Laptops of 2026" not in haystack


class TestTheGateIsPositiveNotMerelyNotBingCom:
    """Five shapes that a "not on bing.com" gate lets through, all measured.

    The first attempt at surviving uBO widened the anchor gate with
    `not(contains(@href, 'bing.com'))`. That is a NEGATIVE test, and a negative
    test fails OPEN on everything its author did not think of. Measured
    2026-09-04 against the shipped preset, every one of these put a believable
    title into the column with no warning a consumer could act on -- and in a
    real SERP, alongside genuine rows, the leaked node is its own container so
    all four columns grow together and `row_alignment_mismatch` cannot fire
    either.

    The worst is the first: an ad whose anchor points straight at the
    advertiser ships a REAL followable link, so a consumer that zips these
    columns and follows them (yozh-law-checker does) gets an advertiser landing
    page presented as an organic result. The second is the exact failure the
    comment on BING_COPILOT_WPT_AD_WITH_H2 predicted and said must never
    happen: `contains('', 'bing.com')` is false for an anchor with NO href at
    all, so `not()` was true and the row was accepted.

    Relying on "an ad's href says bing.com" was doubly wrong, because that fact
    is a property of an UNPINNED, auto-updating third-party filter list -- uBO's
    rule selects on `ck/a` and leaves `aclk` alone TODAY. One upstream edit
    turns every ad into shape 1. So the gate is positive now (Bing's own
    organic redirect, or an absolute http(s) URL off every bing host) and the
    ad exclusion is structural (`mma_acf*`, `b_ads1line`) rather than resting on
    what an ad's href happens to spell.
    """

    LEAKS = {
        "ad pointing straight at the advertiser": (
            '<div class="b_wpt_bl"><div class="mma_acf_label_container">'
            '<span>Gesponsert</span></div><h2><a '
            'href="https://ads.example/landing?msclkid=1">AD direct</a></h2></div>'
        ),
        "anchor with no href attribute at all": (
            '<div class="b_wpt_bl"><h2><a>No href at all</a></h2></div>'
        ),
        "relative in-site href": (
            '<div class="b_wpt_bl"><h2><a href="/videos/search?q=laptop">'
            'Relative row</a></h2></div>'
        ),
        "javascript: href": (
            '<div class="b_wpt_bl"><h2><a href="javascript:void(0)">JS row</a></h2></div>'
        ),
        "uppercase bing host": (
            '<div class="b_wpt_bl"><h2><a href="https://WWW.BING.COM/aclk?ld=e8&amp;'
            'u=aHR0cA">UPPER ad</a></h2></div>'
        ),
        "in-b_algo ad unit EasyList targets by b_ads1line": (
            '<li class="b_algo"><div class="b_title"><h2><span class="b_ads1line">'
            'Anzeige</span><a href="https://ads.example/inline">Inline ad</a>'
            '</h2></div></li>'
        ),
    }

    def setup_method(self):
        self.preset = _load("bing_search_camoufox")

    @pytest.mark.parametrize("label", sorted(LEAKS))
    def test_the_shape_reaches_no_column(self, label):
        html = f"<html><body><ol id=\"b_results\">{self.LEAKS[label]}</ol></body></html>"
        data, _ = extract_fields(html, self.preset.parsing_instructions)
        assert data["titles"] == [], f"{label}: {data['titles']!r}"
        assert data["links"] == [], f"{label}: {data['links']!r}"

    def test_a_real_organic_row_still_passes_the_positive_gate(self):
        """The gate must reject those six WITHOUT rejecting the answer.

        Both accepted shapes, since the preset now meets both in production:
        Bing's own wrapper, and the destination uBO leaves behind."""
        for html in (_CLASSIC_ALGO_BLOCKS[0], _ubo_sanitize(_CLASSIC_ALGO_BLOCKS[0])):
            page = f"<html><body><ol id=\"b_results\">{html}</ol></body></html>"
            data, _ = extract_fields(page, self.preset.parsing_instructions)
            assert data["titles"] == [
                "The Best Laptops We've Tested (August 2026) | PCMag"
            ], html[:60]
            assert data["links"] == ["https://www.pcmag.com/picks/the-best-laptops"]


class TestEveryColumnSelectsFromTheSameCards:
    """A card the gate rejects must leave NO column, not some.

    Review finding, 2026-09-08 (Раиль): the organic test sat at two different
    levels. `titles`/`links` filtered the ANCHOR -- `(cards)//h2//a[gate]` --
    while `snippets`/`result_blocks` filtered the CARD, and only on the
    `b_wpt_bl` branch. So a `li.b_algo` card whose h2 anchor points back at
    bing.com contributed no title and no link, but did contribute a snippet
    and a block: snippets ran one row long and every row after it paired with
    the wrong result. Reproduced before the fix -- titles 2, snippets 3, and
    the second real title zipped against the self-link's snippet.

    `row_alignment_mismatch` cannot save a consumer here: the columns simply
    have different lengths, and the wrong pairing is silent.
    """

    SELF_LINK = (
        '<li class="b_algo"><h2><a href="https://www.bing.com/search?q=more">'
        'More results on Bing</a></h2><div class="b_caption">'
        '<p class="b_lineclamp2">a snippet on a card that links back to bing</p>'
        '</div></li>'
    )

    def _page(self, blocks):
        return f"<html><body><ol id=\"b_results\">{''.join(blocks)}</ol></body></html>"

    @pytest.mark.parametrize("name", ["bing_search_chromium", "bing_search_camoufox"])
    def test_a_self_linking_card_leaves_every_column(self, name):
        preset = _load(name)
        real = _CLASSIC_ALGO_BLOCKS[0]
        data, _ = extract_fields(self._page([real, self.SELF_LINK, real]), preset.parsing_instructions)
        lengths = {col: len(data[col]) for col in ("titles", "links", "snippets", "result_blocks")}
        assert len(set(lengths.values())) == 1, lengths
        assert lengths["titles"] == 2, lengths
        assert all("bing" not in (s or "").lower() or "back to bing" not in (s or "")
                   for s in data["snippets"]), data["snippets"]

    @pytest.mark.parametrize("name", ["bing_search_chromium", "bing_search_camoufox"])
    def test_the_rows_still_pair_correctly_around_the_rejected_card(self, name):
        preset = _load(name)
        real = _CLASSIC_ALGO_BLOCKS[0]
        data, _ = extract_fields(self._page([real, self.SELF_LINK, real]), preset.parsing_instructions)
        # both surviving rows are the real one, each with its own snippet
        assert data["titles"] == ["The Best Laptops We've Tested (August 2026) | PCMag"] * 2
        assert all("Der Markt ist" in s or "PCMag" in s or s.strip()
                   for s in data["snippets"]), data["snippets"]
        assert "back to bing" not in " ".join(data["snippets"])
