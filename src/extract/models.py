from __future__ import annotations

import re
from typing import Any, Dict, Literal

from lxml.cssselect import CSSSelector
from pydantic import BaseModel, Field, model_validator

ExtractType = Literal["css", "xpath"]

# Axes an XPath step can open with and still be unable to leave the node it was
# called on. `descendant-or-self` is what `.//` expands to and what cssselect
# compiles every CSS selector into, which is why CSS needs no equivalent rule:
# measured across `a > h3`, `a:has(h3)`, `div.wrap h3`, `#rso h3`, `body h3`
# and `:root h3`, a CSS selector under a container can only under-match, never
# escape upward.
_ROW_SAFE_XPATH_AXES = frozenset(
    {"self", "child", "descendant", "descendant-or-self", "attribute"}
)


def _top_level_steps(expr: str):
    """Split an XPath on `/`, ignoring slashes inside predicates and strings.

    A naive `expr.split("/")` reads `.//a[descendant::h3]` as a step named
    `a[descendant` and refuses a selector that never leaves the row, and it
    reads the `|` in `.//a[@title="a|b"]` as a union. Both are false
    rejections, which cost an author a working recipe just as surely as a
    missed escape costs a caller a wrong one.
    """
    steps, current, depth, quote = [], [], 0, ""
    for char in expr:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "/" and depth == 0:
            steps.append("".join(current))
            current = []
            continue
        current.append(char)
    steps.append("".join(current))
    return steps


def _outside_brackets(step: str) -> str:
    """`step` with predicate contents and string literals blanked out.

    Reading only up to the first `[` was not enough: `.//h3[1]|//h3` kept its
    union hidden behind the predicate and was accepted. Blanking rather than
    truncating keeps everything after the predicate visible.
    """
    out, depth, quote = [], 0, ""
    for char in step:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "[":
            depth += 1
            continue
        if char == "]":
            depth -= 1
            continue
        if depth == 0:
            out.append(char)
    return "".join(out)


def row_safe_xpath(expr: str) -> str | None:
    """Why `expr` could leave the node it is evaluated on, or None if it cannot.

    Checked per STEP, not by prefix. A prefix check was the first draft and it
    leaked: `..//h3` opens with the blessed `.` and then climbs on the very
    next character. Every rejection below was measured returning values from
    OUTSIDE the row.

    The first step must open with a safe axis, which rules out a
    document-scoped set being filtered afterwards -- `(//h3)[1]`,
    `id('rso')//h3` -- and no later step may be `..` or name an axis that can
    climb or step sideways. Predicates and string literals are skipped, so a
    row-safe expression is not refused for the shape of its filter.
    """
    text = expr.strip()
    if not text:
        return "an empty selector"
    steps = _top_level_steps(text)
    if any(_outside_brackets(step).find("|") >= 0 for step in steps):
        return "a `|` union, because either branch could leave the row"
    first = steps[0].strip()
    # An empty first step is a LEADING SLASH: the absolute path, evaluated from
    # the document root however the call was made.
    if first != "." and not first.startswith(
        ("self::", "child::", "descendant::", "descendant-or-self::", "attribute::")
    ):
        return (
            "a first step that cannot be proven to stay inside the row "
            f"({first!r})"
        )
    for step in steps:
        step = step.strip()
        if step in ("", "."):
            continue
        if step.startswith(".."):
            return "`..`, which is the parent axis"
        head = _outside_brackets(step)
        if "::" in head:
            axis = head.split("::", 1)[0].strip()
            if axis not in _ROW_SAFE_XPATH_AXES:
                return f"the '{axis}' axis, which can leave the row"
    return None


def row_safe_selector(selector: str, kind: str) -> str | None:
    """Same question for either language, answered on the XPath both become.

    CSS was originally exempted on the grounds that `cssselect` compiles to
    `descendant-or-self::`. That is true of descendant and child combinators
    and false of the sibling ones: `.row ~ .row h3` compiles through
    `following-sibling::` and, measured on two adjacent rows, returns the NEXT
    row's value from inside the first one -- with no warning, because every
    column is still the same length. So the check runs on the compiled path
    rather than on a claim about the compiler.
    """
    if kind != "css":
        return row_safe_xpath(selector)
    try:
        compiled = CSSSelector(selector, translator="html").path
    except Exception:  # pylint: disable=broad-exception-caught
        # An unparseable selector is reported per field at extraction time,
        # where every other bad selector is reported; not this guard's job.
        return None
    return row_safe_xpath(compiled)

PostProcessOp = Literal[
    "regex",
    "strip",
    "strip_tags",
    "parse_int",
    "parse_float",
    "parse_price",
    "lowercase",
    "uppercase",
    "replace",
    "base64_decode",
    "urljoin",
    "null_if_regex",
    "unwrap_param",
]


class PostProcess(BaseModel):
    op: PostProcessOp = Field(
        ...,
        description=(
            "Transform applied to the extracted value. Steps run in order, "
            "each fed the previous result. 'regex' (args=[pattern, group?]) "
            "returns a capture group; 'parse_price' (its locale arg is accepted for older presets and ignored: the separator is read from the text) and "
            "'parse_int'/'parse_float' coerce to numbers; 'strip' (args=[chars]?), "
            "'lowercase', 'uppercase', and 'replace' (args=[old, new]) are "
            "string ops. 'base64_decode' decodes url-safe base64 with optional "
            "padding — pair it with 'regex' to read a destination out of a "
            "click-tracking wrapper. 'strip_tags' renders an HTML "
            "fragment down to its "
            "text (tags dropped, entities decoded, whitespace collapsed) — "
            "pair it with attr='html' + 'regex' to read a value out of an "
            "always-present container without the container's markup. "
            "'unwrap_param' (args=[param_name, encoding?, prefix?]) reads a "
            "query parameter out of a click-tracking redirect and decodes it. "
            "`encoding` is 'percent' (the default: Amazon's sponsored-result "
            "links carry the destination inline as "
            "'/sspa/click?...&url=%2Freal%2Fpath') or 'base64url' (Bing hides "
            "every organic URL behind 'bing.com/ck/a?...&u=a1<base64url>'), and "
            "`prefix` is stripped off the value before a base64url decode. "
            "When the value has no such parameter — or carries it without "
            "`prefix`, or with a payload that does not decode — it PASSES "
            "THROUGH UNCHANGED (not null), which is what lets one pipeline handle a "
            "mixed field of wrapped and unwrapped values -- pair it with "
            "'urljoin' to also resolve the recovered (still relative) path. "
            "The decoded value is only ever handed on when it is an absolute "
            "http(s) URL with a host, or a plain relative path: the parameter "
            "is text the PAGE controls, so a crafted value is refused and the "
            "original wrapper passes through UNCHANGED (never null). Refused: "
            "any other scheme ('javascript:', 'data:', 'file:', 'ftp:' ...); "
            "an http(s) value naming no host ('http:///x'); anything opening "
            "with TWO OR MORE slashes or backslashes -- '//host/path' and "
            "every other spelling of it ('///host', '/\\host', '\\\\host'), "
            "which a browser resolves onto 'host' identically; and a "
            "reference naming no path segment of its own ('', '.', '?a=b', "
            "'#frag'), which can only point back at the page being scraped. "
            "Worst case is a passthrough, so this op cannot introduce a "
            "non-http(s) link and cannot turn a relative value into an "
            "off-host one. It is NOT a same-host guarantee: an absolute "
            "http(s) URL naming ANY host is accepted by design, because a "
            "redirect may legitimately point off-site. "
            "'url=https%3A%2F%2Fother.example%2Fx' is handed on as "
            "'https://other.example/x' and a following 'urljoin' leaves it "
            "unchanged (RFC 3986: an absolute reference ignores the base). "
            "A caller that needs the destination to stay on the page's own "
            "host must check the host itself -- this op does not do it for "
            "them, and neither does 'urljoin'. "
            "'urljoin' (args=[base_url]?) resolves a relative href (Amazon "
            "serves every search-result href relative) against base_url via "
            "RFC 3986 resolution; an already-absolute value passes through "
            "unchanged. extract_fields never sees the page's own URL, so "
            "base_url is usually left empty in the preset and injected by "
            "the materializer from the request it is about to fetch. There "
            "is no sensible default transform for a URL with no base: with none at all "
            "(materializer bypassed and no explicit base_url given) it "
            "leaves the value UNCHANGED and adds a warning, rather than "
            "silently shipping a relative link with nothing telling the "
            "caller why. 'null_if_regex' (args=[pattern]) nulls the value "
            "IN PLACE when pattern matches, leaving it untouched otherwise "
            "-- for excluding one shape from an all=true field without "
            "shrinking the array, which would misalign every later row "
            "against its sibling fields."
        ),
    )
    args: list[Any] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_args(self) -> PostProcess:
        # Catch obviously broken op/args combinations at preset-creation time
        # rather than per-row at scrape time. Ops without an arity check
        # accept anything (and ignore extras).
        if self.op == "replace" and len(self.args) < 2:
            raise ValueError("replace requires 2 args: [old, new]")
        if self.op == "regex" and len(self.args) < 1:
            raise ValueError("regex requires 1 arg: [pattern]")
        if self.op == "unwrap_param":
            if len(self.args) < 1:
                raise ValueError("unwrap_param requires 1 arg: [param_name]")
            if len(self.args) > 1 and self.args[1] not in ("percent", "base64url"):
                # Caught here rather than per row: an unknown encoding would
                # otherwise silently fall back to percent-decoding and ship a
                # base64 blob as if it were a destination.
                raise ValueError(
                    "unwrap_param arg 2 must be 'percent' or 'base64url', "
                    f"got {self.args[1]!r}"
                )
        if self.op == "null_if_regex":
            if len(self.args) < 1:
                raise ValueError("null_if_regex requires 1 arg: [pattern]")
            # Unlike 'regex' (whose pattern is validated the same way it has
            # been since this op shipped, and is left alone here), an
            # uncompilable null_if_regex pattern is caught at preset-creation
            # time rather than nulling the whole column at scrape time -- the
            # cost of a self-heal-worthy typo discovered only in production.
            try:
                re.compile(self.args[0])
            except re.error as exc:
                raise ValueError(
                    f"null_if_regex: invalid regex pattern {self.args[0]!r}: {exc}"
                ) from exc
        if self.op == "parse_price":
            if self.args and self.args[0] not in ("us", "eu"):
                raise ValueError(
                    "parse_price locale must be 'us' or 'eu', "
                    f"got {self.args[0]!r}"
                )
        return self


class FieldRule(BaseModel):
    selector: str = Field(
        ...,
        description=(
            "CSS or XPath expression. Defaults to the parent ExtractRule.type "
            "unless `type` below overrides it per-field. Examples: 'h1', "
            "'.price_color', '#cart a' for CSS; '//h1', "
            "'//div[@class=\"item\"]/a/@href' for XPath."
        ),
    )
    type: ExtractType | None = Field(
        default=None,
        description=(
            "Override the parent ExtractRule.type for just this field. Lets a "
            "single rule mix CSS and XPath selectors. Defaults to the rule "
            "type when unset."
        ),
    )
    attr: str = Field(
        default="text",
        description=(
            "What to pull from the matched element. One of: "
            "'text' (default, text content), 'html' (outer HTML of the node), "
            "or any HTML attribute name like 'href', 'src', 'data-id'."
        ),
    )
    all: bool = Field(
        default=False,
        description=(
            "If false (default), returns the FIRST match as a string. If true, "
            "returns a LIST of every match. Use true for repeating elements "
            "like product cards, links, table rows. With `container` set, the "
            "list has one entry per row rather than one per match."
        ),
    )
    required: bool = Field(
        default=False,
        description=(
            "If true and the selector matches nothing, a warning is added to "
            "the response. The request itself still succeeds."
        ),
    )
    post_process: list[PostProcess] = Field(
        default_factory=list,
        description=(
            "Ordered list of transforms applied to the matched value(s) "
            "before they land in the response. Applied per-item when all=true."
        ),
    )


class ExtractRule(BaseModel):
    type: ExtractType = Field(
        ...,
        description=(
            "Default selector language for every field: 'css' "
            "(lxml.cssselect) or 'xpath' (lxml XPath). A field may override "
            "this via its own `type`."
        ),
    )
    fields: Dict[str, FieldRule] = Field(
        ...,
        description=(
            "Map of {output_key: FieldRule}. The output_key is the name the "
            "extracted value will appear under in the response's data object. "
            "Example: {'title': {selector:'h1'}, 'price': {selector:'.price_color'}} "
            "-> data = {'title': '...', 'price': '...'}."
        ),
    )

    container: str | None = Field(
        default=None,
        description=(
            "Selector for the ROW every field in this rule belongs to. When "
            "set, each field's `selector` is evaluated relative to each "
            "matching row and contributes exactly one entry per row -- null "
            "where that row has no match, never a shorter list. All fields "
            "are therefore the same length by construction, so `titles[i]`, "
            "`links[i]` and `snippets[i]` describe the same result. "
            "It sits on the rule, not the field, because a row is a property "
            "of the PAGE: every row-shaped recipe we ship would set the same "
            "value on every one of its fields, and a per-field knob would let "
            "siblings disagree about what a row is -- which is the defect "
            "this exists to remove. With a container every field must set "
            "`all: true`. Example: container '#rso div.tF2Cxc' with fields "
            "selecting 'h3', 'a:has(h3)', 'div.VwiC3b'."
        ),
    )

    @model_validator(mode="after")
    def _container_scoping_is_enforceable(self) -> "ExtractRule":
        """A row-scoped recipe must be one we can prove stays in its row.

        Stated as the POSITIVE shape, deliberately. The first draft rejected
        `selector.startswith("/")` and ten XPath forms walked straight past it
        -- `(//h3)[1]`, `following::h3`, `ancestor::div//h3`, a `|` union, and
        a single leading space among them. That is worse than the defect being
        fixed: with a container every column is the same length by
        construction, so `row_alignment_mismatch` -- a LENGTH check -- can
        never fire, and a selector that leaves its row produces confidently
        mispaired rows with no warning at all.

        So an XPath field selector under a container must BEGIN with an axis
        that cannot climb or step sideways, and may not use `|` (either branch
        could escape) or a leading `(` (a predicate applied to a
        document-scoped set).
        """
        if self.container is None:
            return self
        if not self.container.strip():
            raise ValueError(
                "`container` must be a selector; omit it entirely for a rule "
                "that is not row-scoped"
            )

        for name, field_rule in self.fields.items():
            if not field_rule.all:
                raise ValueError(
                    f"field '{name}': `container` makes every field a column, "
                    f"so it must set `all: true`"
                )
            reason = row_safe_selector(
                field_rule.selector, field_rule.type or self.type
            )
            if reason is not None:
                raise ValueError(
                    f"field '{name}': a selector under `container` must "
                    f"not be able to leave its row, and {field_rule.selector!r} "
                    f"uses {reason}. Use './/' for a descendant at any depth; "
                    f"'.' addresses the row itself. Note that './' and a bare "
                    f"step name are the CHILD axis and match nothing on the "
                    f"nested markup real pages have."
                )
        return self
