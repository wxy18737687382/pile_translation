import re
from typing import Tuple, Dict


class FormatMasker:
    """
    Masks and safely unmasks specialized formatting patterns in academic texts:
    - Display math formulas ($$...$$)
    - Inline math formulas ($...$)
    - Code blocks (```...```) and inline code (`...`)
    - In-text citation markers:
        * Superscript citation markers (<sup>1</sup>, <sup>1,2</sup>, <sup>16–18</sup>, <sup>38,39,53,54</sup>) -> {{CITE_...}}
        * Bracketed citations ([1], [2, 3], [1-5]) -> {{CITE_...}}
      Note: Never consume the preceding English word so that LLMs can translate it accurately into Chinese!
    - Physical units and scientific notation powers (e.g. μm<sup>3</sup>, 10<sup>-4</sup>) -> {{MATH_VAR_...}}
    - Single-letter math variables, powers, and tensor indices (e.g. R<sup>2</sup>, p<sup>α</sup>, ν<sup>+</sup>, ν<sup>−</sup>, R<sup>⊤</sup>) -> {{MATH_VAR_...}}
    - Genotype / allele markers (e.g. Lamc1<sup>+/-</sup>, Zp3-Cre<sup>tg/+</sup>) -> {{MATH_VAR_...}}
    - Math variables with subscripts, fractions, sums (e.g. d<sub>model</sub>, f<sub>B</sub>, k<sub>0</sub>, 1/√d<sub>k</sub>, ∑<sub>i=1</sub>) -> {{MATH_VAR_...}}
    - Author affiliations and footnote symbols (e.g. <sup>*</sup>, <sup>†</sup>, <sup>‡</sup>) -> {{FN_TAG_...}}
    - Markdown links and images
    """

    def __init__(self):
        self.re_code_block = re.compile(r"```[\w-]*\n[\s\S]*?```")
        self.re_math_display = re.compile(r"\$\$(?:\\.|[\s\S])*?\$\$")
        self.re_math_inline = re.compile(r"(?<!\\)\$(?!\$)([^\$\n]+?)(?<!\\)\$")
        self.re_code_inline = re.compile(r"`([^`]+)`")
        self.re_img = re.compile(r"!\[(.*?)\]\((.*?)\)")

        # 1. Physical units and powers (e.g. μm^3, s^-1)
        self.re_unit_power = re.compile(
            r"\b(?:μm|nm|mm|cm|m|km|s|ms|h|kg|g|mg|kDa|mol|L|mL|μL)<sup>\s*[-+]?\d+(?:/\d+)?\s*</sup>"
        )
        # 2. Powers of numbers and scientific notation (e.g. 10^-4, 10^3)
        self.re_num_power = re.compile(
            r"\b\d+<sup>\s*[-+]?\d+(?:/\d+)?\s*</sup>"
        )
        # 3. Powers following brackets/norms/trig functions (e.g. (p - p_0)^2, |p|^2, sin^2)
        self.re_bracket_power = re.compile(
            r"(?:[)\]|]|\b(?:sin|cos|tan))<sup>\s*[-+]?[12345]\s*</sup>"
        )
        # 4. Genotype / allele markers (e.g. Lamc1+/-, Zp3-Cretg/+)
        self.re_gene_allele = re.compile(
            r"\b[A-Za-z0-9_-]+<sup>\s*(?:[+\-−/]+|[A-Za-z0-9/+\-−]+floxed[A-Za-z0-9/+\-−]*|tm\d+[A-Za-z0-9]*|tg/\+)\s*</sup>"
        )
        # 5. Single-letter math variables with powers/tensor indices (e.g. R^2, p^α, ν^+, R^⊤)
        self.re_single_var_sup = re.compile(
            r"(?<![A-Za-z0-9])([A-Za-zα-ωΑ-Ωℒ𝛿∇])<sup>\s*([2345]|\+|-|−|∗|\*|⊤|′+|α|β|γ|\d+/\d+)\s*</sup>"
        )
        # 6. Math variable expressions with subscripts (e.g. d_model, f_B, k_0, 1/√d_k, ∑_{i=1})
        self.re_math_sub = re.compile(
            r"(?:1/√|√|∑|\b[A-Za-zα-ωΑ-Ωℒ𝛿∇]|\b(?:sin|cos|tan|log|exp|sgn|det|tr))(?:<sub>[^<]+</sub>)+(?:<sup>[^<]+</sup>)?"
        )
        # 7. In-text citation superscripts (e.g. <sup>1</sup>, <sup>1,2</sup>, <sup>16–18</sup>, <sup>38,39,53,54</sup>)
        self.re_citation_sup = re.compile(
            r"<sup>\s*(\d+(?:\s*[,–\-]\s*\d+)*)\s*</sup>"
        )
        # 8. Bracketed citations (e.g. [1], [1-3], [2, 3])
        self.re_citation_bracket = re.compile(
            r"\[\s*\d+(?:\s*[,–\-]\s*\d+)*\s*\]"
        )
        # 9. Remaining footnote/affiliation tags (e.g. <sup>*</sup>, <sup>†</sup>)
        self.re_footnote_sup = re.compile(
            r"<sup>[^<]+</sup>|<sub>[^<]+</sub>"
        )

    def mask(self, text: str) -> Tuple[str, Dict[str, str]]:
        """Replaces formulas, script variables, footnotes, code, and citations with safe placeholders"""
        placeholders: Dict[str, str] = {}
        counter = {"val": 0}

        def _make_placeholder(category: str, raw_content: str) -> str:
            token = f"{{{{{category}_{counter['val']}}}}}"
            placeholders[token] = raw_content
            counter["val"] += 1
            return token

        # 1. Protect multi-line code blocks
        text = self.re_code_block.sub(lambda m: _make_placeholder("CODE_BLOCK", m.group(0)), text)

        # 2. Protect display math ($$...$$)
        text = self.re_math_display.sub(lambda m: _make_placeholder("MATH_DISP", m.group(0)), text)

        # 3. Protect images
        text = self.re_img.sub(lambda m: _make_placeholder("IMG", m.group(0)), text)

        # 4. Protect inline math ($...$)
        text = self.re_math_inline.sub(lambda m: _make_placeholder("MATH_INL", m.group(0)), text)

        # 5. Protect inline code (`...`)
        text = self.re_code_inline.sub(lambda m: _make_placeholder("CODE_INL", m.group(0)), text)

        # 6. Protect math/physics terms: unit powers, number powers, bracket powers, genotypes, single-letter powers, subscripted vars
        text = self.re_unit_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_num_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_bracket_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_gene_allele.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_single_var_sup.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_math_sub.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)

        # 7. Protect citation superscripts (e.g. <sup>1</sup>, <sup>16–18</sup>):
        # Never swallow the preceding word; add space if directly attached to an alphanumeric character
        def _cite_sup_sub(m):
            start = m.start()
            lead = " " if (start > 0 and text[start - 1].isalnum()) else ""
            return lead + _make_placeholder("CITE", m.group(0))

        text = self.re_citation_sup.sub(_cite_sup_sub, text)

        # 8. Protect bracketed citations ([1], [1-3])
        text = self.re_citation_bracket.sub(lambda m: _make_placeholder("CITE", m.group(0)), text)

        # 9. Protect remaining footnote tags (e.g. <sup>*</sup>, <sup>†</sup>)
        text = self.re_footnote_sup.sub(lambda m: _make_placeholder("FN_TAG", m.group(0)), text)

        return text, placeholders

    def unmask(self, text: str, placeholders: Dict[str, str]) -> str:
        """Safely restores placeholders with fault tolerance for casing and whitespace variations"""
        if not text or not placeholders:
            return text

        result = text
        # Restore in reverse token order to prevent nested collision
        for token, original in reversed(list(placeholders.items())):
            result = result.replace(token, original)

        for token, original in reversed(list(placeholders.items())):
            inner = token.strip("{}")
            fuzzy_pattern = re.compile(r"\{\{\s*" + re.escape(inner) + r"\s*\}\}", re.IGNORECASE)
            result = fuzzy_pattern.sub(lambda _: original, result)

        # Remove extraneous whitespace mistakenly introduced between word/character and superscript
        result = re.sub(r"([A-Za-z0-9\u4e00-\u9fff\)\]])\s+<sup>", r"\1<sup>", result)
        result = re.sub(r"</sup>\s+([，。！？；：、）》,.!?;:])", r"</sup>\1", result)

        return result
