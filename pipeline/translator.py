import os
import re
import json
import time
import threading
from pathlib import Path
from typing import List, Optional, Dict, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from pipeline.models import DocumentBlock, BlockType
from pipeline.masker import FormatMasker


DEFAULT_SYSTEM_PROMPT = """你是一位顶尖的学术文献翻译专家与资深学者。你的任务是将英文学术文献翻译为高质量的学术中文，做到“信、达、雅”。

【必须严格遵守的铁律】：
1. 占位符绝对保护：输入中包含类似 {{MATH_DISP_0}}、{{MATH_INL_1}}、{{MATH_VAR_2}}、{{FN_TAG_3}}、{{CODE_BLOCK_4}}、{{CITE_5}}、{{IMG_6}} 等占位符。这些代表数学公式、变量上下标、文献角注、代码块、文献引用和图片。你必须原封不动地保留所有占位符及其编号，严禁修改、翻译、删除或凭空添加占位符！
2. 文献引用上标对齐：输入中的 {{CITE_...}} 代表正文文献引用上标（如原文的 <sup>1</sup>, <sup>16–18</sup> 或 [1] 等）。在翻译时，请将 {{CITE_...}} 紧贴在所修饰的翻译后的中文学术术语、概念或句子之后，严禁丢失或移位！
3. Markdown 格式保留：严格保留输入文本中的 Markdown 标记，包括各级标题 (#, ##, ###)、列表标记 (- , • , 1. )、粗体 (**) 等结构语法。
4. 表格、公式与参考文献规范：表格内容、数学公式、以及参考文献条目（[1], [2] 等条目及作者、期刊名、DOI）必须严格保持原文原貌，严禁破坏、修改或翻译！
5. 学术用语规范：符合中文学术规范，使用领域通用的标准中文术语，句式通顺连贯，避免生硬机翻感。
6. 只输出翻译结果，不要输出任何额外的问候、前言、注释或说明文字。
"""


class AcademicTranslator:
    """
    负责调用大语言模型进行学术文本翻译、格式/角注保护与持久化缓存加速
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: str = "deepseek-chat",
        temperature: float = 0.2,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        enable_reflection: bool = False,
        cache_dir: str = "./output",
        para_format: str = "reorganize"
    ):
        self.para_format = para_format
        self.api_key = (
            api_key
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("GEMINI_API_KEY")
        )
        self.base_url = (
            base_url
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
        )
        self.model_name = model_name
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.enable_reflection = enable_reflection
        self.masker = FormatMasker()

        # 缓存配置
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / ".translation_cache.json"
        self._cache_lock = threading.Lock()
        self.cache: Dict[str, str] = self._load_cache()

        # 智能适配 Base URL
        if not self.base_url and self.model_name.startswith("deepseek"):
            self.base_url = "https://api.deepseek.com"
        elif not self.base_url and "gemini" in self.model_name.lower():
            self.base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"

        if not self.api_key or self.api_key.strip() in (
            "", "dummy", "your_api_key_here", "your_gemini_api_key_here", "your_deepseek_api_key_here"
        ):
            raise ValueError(
                "未检测到有效的大模型 API 密钥！\n"
                "请在项目根目录的 .env 文件中设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。\n"
                "💡 提示：若仅测试文档分块、公式保护与排版导出流程，可直接添加 --mock 参数运行模拟模式。"
            )

        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self.client = OpenAI(**client_kwargs)

    def _normalize_cache_key(self, text: str) -> str:
        """生成标准化缓存键"""
        cleaned = re.sub(r"[\s*_#>]+", " ", text).strip().lower()
        return cleaned

    def _is_valid_cached_translation(self, text: str) -> bool:
        """检查缓存译文是否纯净，剔除含未展开占位符、旧版乱码、大模型拒答提示或英文词粘连上标的残次条目"""
        if not text:
            return False
        bad_tokens = (
            "{{", "}}", "~~", "<u>", "_d_ 模型", "<sup>.", "<sup>1</sup>d",
            "!\"#!$", "%&'(", ")!*+", "-?@@", "-?1)", ",,6", "+,-", ".!/+0!", "#*1!"
        )
        if any(tok in text for tok in bad_tokens):
            return False
        # 严格过滤大模型在极短文本时的模板拒答反问
        if any(bad in text for bad in [
            "由于您只提供", "请提供需要翻译", "没有完整的学术内容", "无法提供翻译",
            "未提供需要翻译", "请提供待翻译", "没有提供任何内容"
        ]):
            return False
        # 严格过滤英文普通单词粘连上标的旧缓存（如 organization<sup>1</sup>, signalling<sup>16 等）
        if re.search(r"[a-zA-Z]{3,}\s*<sup>\d+", text):
            return False
        return True

    def _load_cache(self) -> Dict[str, str]:
        data: Dict[str, str] = {}
        if self.cache_file.exists():
            try:
                raw_data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                data = {
                    k: v for k, v in raw_data.items()
                    if self._is_valid_cached_translation(v)
                    and self._is_valid_cached_translation(k)
                    and "<sup>1</sup>d" not in k
                }
            except Exception:
                data = {}

        # 自动从已有的 *_bilingual.md 提取历史纯净译文以加速重复运行
        for md_file in self.cache_dir.glob("*_bilingual.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                pairs = re.findall(
                    r">\s*\*\*\[EN\]\*\*\s*(.+?)\n>\n>\s*\*\*\[ZH\]\*\*\s*(.+?)(?:\n\n|\Z)",
                    content,
                    flags=re.DOTALL
                )
                for en_raw, zh_raw in pairs:
                    en_s = en_raw.strip()
                    zh_s = zh_raw.strip()
                    if not self._is_valid_cached_translation(zh_s) or not self._is_valid_cached_translation(en_s):
                        continue
                    # 仅复用不含复杂上下标的纯文本段落缓存
                    if "<sub" not in en_s and "<sup" not in en_s:
                        norm_k = self._normalize_cache_key(en_s)
                        if norm_k and norm_k not in data:
                            data[norm_k] = zh_s
            except Exception:
                pass
        return data

    def _save_cache(self):
        try:
            self.cache_file.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def translate_block(self, block: DocumentBlock) -> str:
        """翻译单个 DocumentBlock"""
        # 1. 对代码块、分割线、独立数学公式、图片、页码、作者栏、参考文献直接保留原文
        if block.type in (
            BlockType.CODE_BLOCK,
            BlockType.DIVIDER,
            BlockType.MATH_BLOCK,
            BlockType.IMAGE,
            BlockType.RAW,
            BlockType.AUTHOR_INFO,
            BlockType.REFERENCE,
        ) or (block.metadata and (block.metadata.get("is_reference") or block.metadata.get("is_ref_continuation") or block.metadata.get("skip_translation") or block.metadata.get("is_sidebar"))):
            block.translated_text = block.original_text
            return block.original_text

        # 2. 对公式间常见的极短过渡介词/连词直接规范化翻译，避免大模型拒答反问
        short_connectives = {
            "with": "其中",
            "where": "其中",
            "and": "且",
            "or": "或",
            "for": "对于",
            "in": "在",
            "such that": "使得",
            "given by": "由下式给出",
            "here,": "此处，",
            "here": "此处",
        }
        strip_clean = block.original_text.strip().rstrip(".").lower()
        if strip_clean in short_connectives:
            block.translated_text = short_connectives[strip_clean]
            return block.translated_text

        # 3. 检查精确文本缓存（带完整 HTML 上下标标签的精确匹配）
        exact_key = f"EXACT::{block.original_text.strip()}"
        with self._cache_lock:
            if exact_key in self.cache:
                block.translated_text = self.cache[exact_key]
                return block.translated_text
            # 若本块不含任何上下标或角注标签，也可匹配归一化缓存
            if "<sub" not in block.original_text and "<sup" not in block.original_text:
                norm_key = self._normalize_cache_key(block.original_text)
                if norm_key in self.cache and block.type == BlockType.PARAGRAPH:
                    block.translated_text = self.cache[norm_key]
                    return block.translated_text

        # 4. 提取并屏蔽特殊格式 (公式、上下标变量、角注、引用、行内代码等)
        masked_text, placeholders = self.masker.mask(block.original_text)
        block.placeholders = placeholders

        # 5. 调用大模型进行初译
        raw_translation = self._call_llm(masked_text)

        # 过滤模型在单词/极短文本时的拒答或反问
        if any(bad in raw_translation for bad in [
            "由于您只提供", "请提供需要翻译", "没有完整的学术内容", "无法提供翻译",
            "未提供需要翻译", "请提供待翻译", "没有提供任何内容"
        ]):
            raw_translation = short_connectives.get(strip_clean, block.original_text)

        # 6. 可选：反思润色环节
        final_translation = raw_translation
        if self.enable_reflection and block.type == BlockType.PARAGRAPH and len(masked_text) > 80:
            final_translation = self._reflect_and_polish(masked_text, raw_translation)

        # 7. 安全还原占位符
        unmasked = self.masker.unmask(final_translation, placeholders)
        block.translated_text = unmasked

        with self._cache_lock:
            self.cache[exact_key] = unmasked

        return unmasked

    def _translate_cross_page_pair(self, tail_b: DocumentBlock, head_b: DocumentBlock):
        """
        换页断句/断词跨页协同翻译：
        将跨页被截断的句子/段落合并为完整的语义上下文送入大模型，
        彻底根除断词 (如 la- \n tent) 被误当作两句甚至翻出“帐篷扩散模型”的机翻灾难。
        翻译完成后，在保证语义完整连贯的前提下，智能寻找中文句式标点边界进行合理分割，
        分别回填至原版 PDF 各自页面的坐标框内。
        """
        if tail_b.translated_text and head_b.translated_text and tail_b.metadata.get("full_combined_zh"):
            return

        tail_src = tail_b.original_text.strip()
        head_src = head_b.original_text.strip()
        combined_source = tail_b.metadata.get("full_combined_text") or f"{tail_src} {head_src}"

        # 临时封装为一个整体段落执行学术翻译与占位符保护
        temp_block = DocumentBlock(
            id=f"{tail_b.id}_crosspage_pair",
            type=BlockType.PARAGRAPH,
            original_text=combined_source,
            level=tail_b.level,
            page_num=tail_b.page_num
        )
        combined_zh = self.translate_block(temp_block)

        # 比例与标点边界切分
        len_tail = len(tail_src)
        len_head = len(head_src)
        ratio = len_tail / max(1, (len_tail + len_head))
        target_pos = int(len(combined_zh) * ratio)

        # 在切分目标点前后寻找最自然的标点断句位置 (优先句号/感叹号/问号，其次分号/冒号，最后逗号)
        window = max(10, int(len(combined_zh) * 0.35))
        min_p = max(0, target_pos - window)
        max_p = min(len(combined_zh), target_pos + window)

        best_idx = -1
        best_priority = 0

        for p_idx in range(min_p, max_p):
            ch = combined_zh[p_idx]
            if ch in ("。", "！", "？", "."):
                priority = 4
            elif ch in ("；", ";", "：", ":"):
                priority = 3
            elif ch in ("，", ","):
                priority = 2
            elif ch in (" ", "\u3000"):
                priority = 1
            else:
                priority = 0

            if priority > best_priority or (priority == best_priority and priority > 0 and abs(p_idx - target_pos) < abs(best_idx - target_pos)):
                best_priority = priority
                best_idx = p_idx

        if best_idx != -1 and best_priority >= 2:
            tail_zh = combined_zh[:best_idx + 1].strip()
            head_zh = combined_zh[best_idx + 1:].strip().lstrip("，,；;。！!？?")
        else:
            tail_zh = combined_zh[:target_pos].strip()
            head_zh = combined_zh[target_pos:].strip()

        tail_b.translated_text = tail_zh
        head_b.translated_text = head_zh
        tail_b.metadata["full_combined_zh"] = combined_zh
        head_b.metadata["full_combined_zh"] = combined_zh
        head_b.metadata["already_translated_with_tail"] = True

    def translate_blocks_concurrently(
        self,
        blocks: List[DocumentBlock],
        max_workers: int = 8,
        on_progress: Optional[Callable[[], None]] = None
    ):
        """多线程并发翻译所有块，大幅缩短整篇 PDF 的等待时间"""
        id_map = {b.id: b for b in blocks}
        pair_lock = threading.Lock()

        def _worker(b: DocumentBlock):
            try:
                if b.metadata.get("already_translated_with_tail"):
                    return
                # 检查是否为跨页协同翻译对
                if b.is_cross_page_tail and b.cross_page_partner_id and b.cross_page_partner_id in id_map:
                    partner = id_map[b.cross_page_partner_id]
                    with pair_lock:
                        if not b.translated_text:
                            self._translate_cross_page_pair(b, partner)
                elif b.is_cross_page_head and b.cross_page_partner_id and b.cross_page_partner_id in id_map:
                    partner = id_map[b.cross_page_partner_id]
                    with pair_lock:
                        if not b.translated_text:
                            self._translate_cross_page_pair(partner, b)
                else:
                    self.translate_block(b)
            except Exception as e:
                b.translated_text = b.original_text
            finally:
                if on_progress:
                    on_progress()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_worker, b) for b in blocks]
            for f in as_completed(futures):
                f.result()

        with self._cache_lock:
            self._save_cache()

    def _call_llm(self, text: str) -> str:
        """调用大模型单次生成，内置重试与容错机制"""
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    temperature=self.temperature,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": f"请将以下学术内容翻译为中文：\n\n{text}"}
                    ]
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    raise last_error

    def _reflect_and_polish(self, source_masked: str, draft_trans: str) -> str:
        """两阶段反思润色机制"""
        reflection_prompt = f"""你是一位资深学术期刊审稿人兼中文编辑。请审校以下学术翻译初稿，进行润色提升。
要求：
1. 修正任何生硬或欧化的句式，使其更符合中文学术期刊规范。
2. 绝对保留原文中所有的占位符（如 {{MATH_...}}、{{FN_TAG_...}}、{{CITE_...}} 等），严禁删改。
3. 严格保留原有 Markdown 排版语法。
4. 只返回润色后的最终译文，不要返回任何批注或解释。

【英文原文（含占位符）】：
{source_masked}

【初译稿】：
{draft_trans}
"""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_name,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": "你是一位专注于学术严谨性与语体润色的审校专家。"},
                        {"role": "user", "content": reflection_prompt}
                    ]
                )
                return response.choices[0].message.content.strip()
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    return draft_trans
