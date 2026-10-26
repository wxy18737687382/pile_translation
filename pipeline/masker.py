import re
from typing import Tuple, Dict


class FormatMasker:
    """
    负责学术文献中特殊格式的屏蔽（占位符化）与安全还原：
    - 独立数学公式 ($$...$$)
    - 行内数学公式 ($...$)
    - 代码块 (```...```) 与行内代码 (`...`)
    - 正文文献引用标记：
        * 上标角注引用 (<sup>1</sup>, <sup>1,2</sup>, <sup>16–18</sup>, <sup>38,39,53,54</sup>) -> {{CITE_...}}
        * 方括号引用 ([1], [2, 3], [1-5]) -> {{CITE_...}}
      注意：严禁将文献引用上标前面的普通英文单词吞噬进占位符，保证英文词能被大模型精准翻译为中文！
    - 物理单位幂次与纯数字科学计数法幂次 (如 μm<sup>3</sup>, 10<sup>-4</sup>) -> {{MATH_VAR_...}}
    - 行内单字母数学变量幂次与张量指标 (如 R<sup>2</sup>, p<sup>α</sup>, ν<sup>+</sup>, ν<sup>−</sup>, R<sup>⊤</sup>) -> {{MATH_VAR_...}}
    - 基因型/等位基因标记 (如 Lamc1<sup>+/-</sup>, Zp3-Cre<sup>tg/+</sup>) -> {{MATH_VAR_...}}
    - 行内带下标的数学变量/分式/求和式 (如 d<sub>model</sub>, f<sub>B</sub>, k<sub>0</sub>, 1/√d<sub>k</sub>, ∑<sub>i=1</sub>) -> {{MATH_VAR_...}}
    - 作者单位与符号脚注 (如 <sup>∗</sup>, <sup>†</sup>, <sup>‡</sup>) -> {{FN_TAG_...}}
    - Markdown 链接与图片
    """

    def __init__(self):
        self.re_code_block = re.compile(r"```[\w-]*\n[\s\S]*?```")
        self.re_math_display = re.compile(r"\$\$(?:\\.|[\s\S])*?\$\$")
        self.re_math_inline = re.compile(r"(?<!\\)\$(?!\$)([^\$\n]+?)(?<!\\)\$")
        self.re_code_inline = re.compile(r"`([^`]+)`")
        self.re_img = re.compile(r"!\[(.*?)\]\((.*?)\)")

        # 1. 物理单位与幂次 (如 μm^3, s^-1)
        self.re_unit_power = re.compile(
            r"\b(?:μm|nm|mm|cm|m|km|s|ms|h|kg|g|mg|kDa|mol|L|mL|μL)<sup>\s*[-+]?\d+(?:/\d+)?\s*</sup>"
        )
        # 2. 数字底数的幂次与科学计数法 (如 10^-4, 10^3)
        self.re_num_power = re.compile(
            r"\b\d+<sup>\s*[-+]?\d+(?:/\d+)?\s*</sup>"
        )
        # 3. 括号/范数/三角函数闭合后的幂次 (如 (p - p_0)^2, |p|^2, sin^2)
        self.re_bracket_power = re.compile(
            r"(?:[)\]|]|\b(?:sin|cos|tan))<sup>\s*[-+]?[12345]\s*</sup>"
        )
        # 4. 基因型/等位基因标记 (如 Lamc1+/-, Zp3-Cretg/+)
        self.re_gene_allele = re.compile(
            r"\b[A-Za-z0-9_-]+<sup>\s*(?:[+\-−/]+|[A-Za-z0-9/+\-−]+floxed[A-Za-z0-9/+\-−]*|tm\d+[A-Za-z0-9]*|tg/\+)\s*</sup>"
        )
        # 5. 单字母数学变量的幂次/张量指标 (如 R^2, p^α, ν^+, R^⊤)
        self.re_single_var_sup = re.compile(
            r"(?<![A-Za-z0-9])([A-Za-zα-ωΑ-Ωℒ𝛿∇])<sup>\s*([2345]|\+|-|−|∗|\*|⊤|′+|α|β|γ|\d+/\d+)\s*</sup>"
        )
        # 6. 带下标的数学变量表达式 (如 d_model, f_B, k_0, 1/√d_k, ∑_{i=1})
        self.re_math_sub = re.compile(
            r"(?:1/√|√|∑|\b[A-Za-zα-ωΑ-Ωℒ𝛿∇]|\b(?:sin|cos|tan|log|exp|sgn|det|tr))(?:<sub>[^<]+</sub>)+(?:<sup>[^<]+</sup>)?"
        )
        # 7. 正文文献引用上标 (如 <sup>1</sup>, <sup>1,2</sup>, <sup>16–18</sup>, <sup>38,39,53,54</sup>)
        self.re_citation_sup = re.compile(
            r"<sup>\s*(\d+(?:\s*[,–\-]\s*\d+)*)\s*</sup>"
        )
        # 8. 方括号文献引用 (如 [1], [1-3], [2, 3])
        self.re_citation_bracket = re.compile(
            r"\[\s*\d+(?:\s*[,–\-]\s*\d+)*\s*\]"
        )
        # 9. 剩余作者单位/角注标签 (如 <sup>∗</sup>, <sup>†</sup>)
        self.re_footnote_sup = re.compile(
            r"<sup>[^<]+</sup>|<sub>[^<]+</sub>"
        )

    def mask(self, text: str) -> Tuple[str, Dict[str, str]]:
        """将文本中的公式、上下标变量、角注、代码、引用等替换为安全占位符"""
        placeholders: Dict[str, str] = {}
        counter = {"val": 0}

        def _make_placeholder(category: str, raw_content: str) -> str:
            token = f"{{{{{category}_{counter['val']}}}}}"
            placeholders[token] = raw_content
            counter["val"] += 1
            return token

        # 1. 保护多行代码块
        text = self.re_code_block.sub(lambda m: _make_placeholder("CODE_BLOCK", m.group(0)), text)

        # 2. 保护独立公式 ($$...$$)
        text = self.re_math_display.sub(lambda m: _make_placeholder("MATH_DISP", m.group(0)), text)

        # 3. 保护图片
        text = self.re_img.sub(lambda m: _make_placeholder("IMG", m.group(0)), text)

        # 4. 保护行内公式 ($...$)
        text = self.re_math_inline.sub(lambda m: _make_placeholder("MATH_INL", m.group(0)), text)

        # 5. 保护行内代码 (`...`)
        text = self.re_code_inline.sub(lambda m: _make_placeholder("CODE_INL", m.group(0)), text)

        # 6. 保护真实数学物理量：单位幂次、纯数字幂、括号幂、基因型、单字母幂、下标变量
        text = self.re_unit_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_num_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_bracket_power.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_gene_allele.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_single_var_sup.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)
        text = self.re_math_sub.sub(lambda m: _make_placeholder("MATH_VAR", m.group(0)), text)

        # 7. 保护文献引用上标 (如 <sup>1</sup>, <sup>16–18</sup>)：
        # 绝不吞噬前方英文单词，若紧贴字母/数字则前置空格，确保大模型精准翻译前面的英文单词
        def _cite_sup_sub(m):
            start = m.start()
            lead = " " if (start > 0 and text[start - 1].isalnum()) else ""
            return lead + _make_placeholder("CITE", m.group(0))

        text = self.re_citation_sup.sub(_cite_sup_sub, text)

        # 8. 保护方括号文献引用 ([1], [1-3])
        text = self.re_citation_bracket.sub(lambda m: _make_placeholder("CITE", m.group(0)), text)

        # 9. 保护剩余角注上标 (如 <sup>∗</sup>, <sup>†</sup>)
        text = self.re_footnote_sup.sub(lambda m: _make_placeholder("FN_TAG", m.group(0)), text)

        return text, placeholders

    def unmask(self, text: str, placeholders: Dict[str, str]) -> str:
        """安全还原占位符，具备容错机制（针对大模型可能插入的空格或大小写变异）"""
        if not text or not placeholders:
            return text

        result = text
        # 按 token 逆序还原，防止嵌套占位符冲突
        for token, original in reversed(list(placeholders.items())):
            result = result.replace(token, original)

        for token, original in reversed(list(placeholders.items())):
            inner = token.strip("{}")
            fuzzy_pattern = re.compile(r"\{\{\s*" + re.escape(inner) + r"\s*\}\}", re.IGNORECASE)
            result = fuzzy_pattern.sub(lambda _: original, result)

        # 清除单词/汉字与上标之间误插入的空格，以及上标与常见标点之间的空格
        result = re.sub(r"([A-Za-z0-9\u4e00-\u9fff\)\]])\s+<sup>", r"\1<sup>", result)
        result = re.sub(r"</sup>\s+([，。！？；：、）》,.!?;:])", r"</sup>\1", result)

        return result
