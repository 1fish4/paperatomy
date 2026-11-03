"""文献网络：引用关系的真实数据层。

不变式：**节点永远不经过模型。**
它们只来自两个可当场验证的来源——

1. PDF 里印刷的参考文献列表（``parse`` 切出的 References 章节）；
2. OpenAlex 的 API 响应（2.5 亿篇真实文献的开放索引）。

相关度是**计算值**并附带解释（共引数、关键词重合），
不是模型的判断。模型在这个模块里没有任何角色——
「推荐的文章 100% 存在」就是这样保证的：不存在的东西
根本进不了这层代码。

为什么中心论文不能依赖 OpenAlex
------------------------------
实测 2026 年的新论文（SPADE、Carbon-Aware）在 OpenAlex 里搜不到——
索引有滞后。所以中心节点只用本地 PDF；周边节点以 PDF 的
参考文献为骨架，OpenAlex 命中的才补全 DOI、年份、被引数，
未命中的节点照样保留（只是元数据少一些）。

网络为什么在生成时抓取而不是打开报告时查询
------------------------------------------
一篇论文的引用关系是**静态事实**，不会过时。生成时抓一次嵌进报告，
离线能看、答辩演示不会因为会场网络翻车——这符合本项目
「单文件、零运行时依赖」的一贯哲学。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

__all__ = ["RefNode", "split_references", "extract_title", "build_network"]

#: OpenAlex 的礼让间隔（无 key 限速 10/s，我们只用一半）。
_REQUEST_GAP = 0.18

#: 直连失败后改走的本地代理端口（与本项目其他联网组件一致）。
_PROXY = "http://127.0.0.1:7897"

#: 节点标题候选里一眼就该跳过的片段（会议页码、URL、出版社）。
_NOISE_RE = re.compile(
    r"(https?://|doi\.org|In Proc|Proceedings|Vol\.|pp\.|arXiv preprint)", re.I)

#: 常见停用词——标题关键词重合计算时排除。
_STOP = {"a", "an", "the", "for", "of", "in", "on", "and", "with", "to",
         "via", "by", "at", "from", "based", "using", "towards", "toward"}


# ---------------------------------------------------------------------------
# References 切条与标题提取
# ---------------------------------------------------------------------------

def split_references(text: str) -> list[str]:
    """把 References 章节文本切成一条条文献。

    首选按 ``[n]`` 编号切；编号被 PDF 拆散时退回启发式：
    每条以「作者. 标题.」开头，编号行之间的内容归并到上一条。
    """
    text = text.strip()
    if not text:
        return []

    # 主路径：[1] ... [2] ... 编号切分。
    # 阈值 2：两条编号即可确认格式（短论文可能只有两三条参考文献）；
    # 正文里偶然出现的 [1] 不会连成递增序列，误入概率极低。
    marks = list(re.finditer(r"\[(\d{1,3})\]", text))
    if len(marks) >= 2:
        entries = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():end].strip()
            body = re.sub(r"\s*\n\s*", " ", body)
            if len(body) >= 30:
                entries.append(body)
        return entries

    # 退路：无编号。按「。+ 大写字母开头」的句界近似切，段落即条目
    chunks = [re.sub(r"\s*\n\s*", " ", c).strip()
              for c in re.split(r"\n(?=[A-Z])", text)]
    return [c for c in chunks if len(c) >= 60]


def _merge_hyphen_breaks(entry: str) -> str:
    """合并 PDF 断行连字符：``Holis- tic`` → ``Holistic``。

    只合并「连字符 + 空格 + 小写」——真正的复合词
    （``Carbon-Aware``）连字符直接连着字母，不受影响。
    """
    return re.sub(r"([a-z])-\s+([a-z])", r"\1\2", entry)


#: 句号分段：句号前是小写/数字/引号、句号后接大写——
#: A02 这类 PDF 会丢空格（``Datacenters.In``），所以不能依赖句号后有空格。
_SENT_SPLIT = re.compile(r"(?<=[a-z0-9)\"\u201d])\.\s*(?=[A-Z\"\u201c])")

#: 判定 venue/URL/年份段的特征——这些段不是标题。
_VENUE_RE = re.compile(
    r"(In Proc|Proceedings|Transactions|Journal|Conference|Workshop|"
    r"arXiv|ACM|IEEE|USENIX|Springer|Wiley|Elsevier|Vol\.|pp\.|"
    r"https?://|doi\.org)", re.I)


def extract_title(entry: str) -> str:
    """从一条参考文献里抠出候选标题。

    顺序：引号内 > 句号分段后「非作者、非 venue、非年份」的最长段。
    抠不出来返回空串——宁缺毋滥，拿作者名或 venue 去搜索
    只会制造错误匹配，错误匹配比缺失危害大。
    """
    entry = _merge_hyphen_breaks(entry.strip())

    m = re.search(r"[\u201c\"]([^\u201d\"]{15,200})[\u201d\"]", entry)
    if m:
        return _clean_title(m.group(1))

    parts = [p.strip(" ,;") for p in _SENT_SPLIT.split(entry)]
    best = ""
    for part in parts:
        if len(part) < 20 or len(part) > 220:
            continue
        # 作者列表特征：高密度逗号（丢空格的 PDF 里「A. Author, Carbon-...」
        # 会粘成一整段，此时它正是标题候选，不能按位置一刀切跳过第一段）
        if part.count(",") >= 3:
            continue
        if _VENUE_RE.search(part):
            continue
        # 年份不是一票否决：ACM 格式里「标题, 2023.」年份粘在标题段尾部，
        # 剥掉年份后剩下的就是标题；剥完不剩什么，才说明整段是 venue+年份。
        ym = re.search(r"\b(19|20)\d{2}\b", part)
        if ym:
            stripped = (part[:ym.start()] + part[ym.end():]).strip(" ,;.")
            part = stripped if len(stripped) >= 20 else part
            if len(stripped) < 20:
                continue
        if part.isupper():
            continue
        if len(part) > len(best):
            best = part
    return _clean_title(best)


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*\n\s*", " ", title).strip(" .,;:")
    # PDF 提取可能丢词间空格（BilgeAcun）：词平均长度异常时按
    # 大小写边界重新切（CamelCase 是丢空格的重灾区）
    words = title.split()
    if words and sum(len(w) for w in words) / len(words) > 11:
        title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title)
    return title


# ---------------------------------------------------------------------------
# OpenAlex 客户端
# ---------------------------------------------------------------------------

_OFFICIAL_TYPES = {"journal", "conference"}


def _pick_venue(work: dict) -> dict:
    """从 works 的多个 location 里挑「实际发表位置」。

    OpenAlex 的 primary_location 常是 arXiv 等预印本仓库（论文的第一
    公开版本），但读者想知道的是「发在哪」——优先期刊/会议的正式
    版本；只有通篇没有正式版本时才退回预印本。
    """
    locs = [l for l in (work.get("locations") or []) if l]
    prim = (work.get("primary_location") or {}) or {}
    official = [
        l for l in locs
        if ((l.get("source") or {}).get("type") or "") in _OFFICIAL_TYPES
        and (l.get("source") or {}).get("display_name")
    ]
    if official:
        # 有多个正式版本时取被引场景最典型的（OpenAlex 顺序即权威度）
        return official[0].get("source") or {}
    return prim.get("source") or {}


@dataclass
class OpenAlex:
    """极简客户端：直连失败自动走本地代理，礼让限速。"""

    gap: float = _REQUEST_GAP
    proxy: str | None = _PROXY
    _last: float = 0.0
    _opener: object = None
    ok: bool = True
    """任一请求成功即 True；全失败时调用方应整体放弃而不是硬凑。"""

    def _fetch(self, url: str) -> dict | None:
        now = time.monotonic()
        wait = self.gap - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

        if self._opener is None:
            handlers: list = []
            if self.proxy:
                handlers.append(urllib.request.ProxyHandler({
                    "http": self.proxy, "https": self.proxy}))
            self._opener = urllib.request.build_opener(*handlers)

        last_err: Exception | None = None
        for use_proxy in (False, True) if self.proxy else (False,):
            try:
                opener = self._opener if use_proxy else \
                    urllib.request.build_opener()
                req = urllib.request.Request(url, headers={
                    "User-Agent": "paperatomy/0.6 (open-source paper reader)"})
                with opener.open(req, timeout=20) as resp:
                    self.ok = True
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429：短时突发限流，退避后原样重试一次（仍失败才降级）
                if exc.code == 429 and last_err != "backoff":
                    last_err = "backoff"
                    time.sleep(2.5)
                    try:
                        with opener.open(req, timeout=20) as resp:
                            self.ok = True
                            return json.loads(resp.read().decode("utf-8"))
                    except Exception as exc2:  # noqa: BLE001
                        last_err = exc2
                        continue
                last_err = exc
            except Exception as exc:  # noqa: BLE001 —— 网络失败逐级回退
                last_err = exc
        self.ok = False
        return None

    def match_title(self, title: str) -> dict | None:
        """按标题匹配一篇文献。返回规范化记录或 None。"""
        if len(title) < 12:
            return None
        url = ("https://api.openalex.org/works?filter=title.search:"
               + urllib.parse.quote(title[:120])
               + "&per_page=3&select=id,doi,title,display_name,"
                 "publication_year,cited_by_count,referenced_works,"
                 "primary_location,locations,topics,keywords")
        data = self._fetch(url)
        if not data:
            return None
        want = _norm(title)
        best, best_score = None, 0.0
        for w in data.get("results", []):
            got = _norm(w.get("display_name") or w.get("title") or "")
            if not got:
                continue
            # 标题重合率：匹配错文献比匹配不到危害大，阈值要高
            a, b = set(want.split()), set(got.split())
            if not a or not b:
                continue
            long_cover = len(a & b) / max(len(a), len(b))
            # PDF 提取的标题常带着尾渣（venue 残片、句点），比库内标题长——
            # 只要「较短一方」被完整覆盖，也算强匹配（略降权）。
            short_cover = len(a & b) / min(len(a), len(b))
            score = max(long_cover, short_cover * 0.92)
            if score > best_score:
                best, best_score = w, score
        if best is None or best_score < 0.75:
            return None
        loc = _pick_venue(best)
        return {
            "openalex_id": (best.get("id") or "").rsplit("/", 1)[-1],
            "doi": (best.get("doi") or "").replace("https://doi.org/", ""),
            "title": best.get("display_name") or best.get("title") or "",
            "year": int(best.get("publication_year") or 0),
            "cited_by": int(best.get("cited_by_count") or 0),
            "venue": (loc.get("display_name") or "")[:80],
            "venue_is_preprint": (loc.get("type") or "")
                                  in ("preprint", "repository"),
            "refs": best.get("referenced_works") or [],
            "topics": [
                {"name": tp.get("display_name") or "", "score": tp.get("score") or 0,
                 "field": (tp.get("field") or {}).get("display_name") or "",
                 "subfield": (tp.get("subfield") or {}).get("display_name") or ""}
                for tp in sorted(best.get("topics") or [],
                                 key=lambda x: -(x.get("score") or 0))[:3]
            ],
            "keywords": [
                str(k.get("display_name") or "").strip()
                for k in (best.get("keywords") or [])
                if k.get("display_name")
            ][:6],
            "match": round(best_score, 2),
        }

    def citing(self, work_id: str, limit: int = 40) -> list[dict]:
        """引用了某篇的论文，按被引数降序。"""
        url = ("https://api.openalex.org/works?filter=cites:" + work_id
               + f"&sort=cited_by_count:desc&per_page={min(limit, 50)}"
               + "&select=id,doi,display_name,publication_year,"
                 "cited_by_count,primary_location")
        data = self._fetch(url)
        out = []
        for w in (data or {}).get("results", []):
            loc = (w.get("primary_location") or {}).get("source") or {}
            out.append({
                "openalex_id": (w.get("id") or "").rsplit("/", 1)[-1],
                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "title": w.get("display_name") or "",
                "year": int(w.get("publication_year") or 0),
                "cited_by": int(w.get("cited_by_count") or 0),
                "venue": (loc.get("display_name") or "")[:80],
                "refs": [],
            })
        return out


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# ---------------------------------------------------------------------------
# CCF 推荐目录（内置数据，真实列表，非模型生成）
# ---------------------------------------------------------------------------

_CCF_INDEX = None


def _ccf_index() -> dict:
    """懒加载内置 CCF 目录 → {规范化名称: 记录}。

    数据来自 CCF 推荐目录（经 myhuiban.com 快照整理），随仓库分发；
    查询命中与不命中都是真实状态——未列入 CCF 目录的 venue 不标等级。
    """
    global _CCF_INDEX
    if _CCF_INDEX is None:
        from pathlib import Path

        p = Path(__file__).resolve().parent / "data" / "ccf.json"
        idx = {}
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))["rows"]
        except Exception:
            rows = []
        for r in rows:
            for name in (r.get("n"), r.get("a")):
                key = _norm(name)
                if key and key not in idx:
                    idx[key] = r
        _CCF_INDEX = idx
    return _CCF_INDEX


def ccf_lookup(venue: str) -> dict | None:
    """venue 名 → CCF 记录（等级/缩写/影响因子）。支持全称与缩写。"""
    if not venue:
        return None
    idx = _ccf_index()
    key = _norm(venue)
    if key in idx:
        return idx[key]
    # venue 名常带年份后缀（``AAAI '25``）或社方全称前后缀，子串兜底
    for k, v in idx.items():
        if not k or min(len(k), len(key)) < 6:
            continue
        if k in key or key in k:
            return v
    return None




# ---------------------------------------------------------------------------
# 相关度：可解释的计算值
# ---------------------------------------------------------------------------

# OpenAlex field/subfield 英→中：只翻译 OpenAlex 已有的标注，属翻译而非判断。
# 未列入的层级原样展示英文，不硬凑。
_FIELD_ZH = {
    "Computer Science": "计算机科学",
    "Mathematics": "数学",
    "Physics": "物理学",
    "Chemistry": "化学",
    "Biology": "生物学",
    "Medicine": "医学",
    "Materials Science": "材料科学",
    "Engineering": "工程学",
    "Environmental Science": "环境科学",
    "Geology": "地质学",
    "Geography": "地理学",
    "Social Sciences": "社会科学",
    "Psychology": "心理学",
    "Economics": "经济学",
    "Business": "商学",
    "Education": "教育学",
    "Law": "法学",
    "Philosophy": "哲学",
    "Political science": "政治学",
    "Art": "艺术学",
    "History": "历史学",
    "Literature": "文学",
    "Linguistics": "语言学",
}
_SUBFIELD_ZH = {
    "Artificial Intelligence": "人工智能",
    "Computational Theory and Mathematics": "计算理论与数学",
    "Computer Vision and Pattern Recognition": "计算机视觉与模式识别",
    "Computer Networks and Communications": "计算机网络与通信",
    "Computer Security and Reliability": "计算机安全与可靠性",
    "Control and Systems Engineering": "控制与系统工程",
    "Distributed and Parallel Computing": "分布式与并行计算",
    "Human-Computer Interaction": "人机交互",
    "Hardware and Architecture": "硬件与体系结构",
    "Information Systems": "信息系统",
    "Software": "软件",
    "Theoretical Computer Science": "理论计算机科学",
    "Electrical and Electronic Engineering": "电子电气工程",
    "Signal Processing": "信号处理",
    "Library and Information Science": "图书情报",
    "Applied Mathematics": "应用数学",
    "Statistics and Probability": "统计与概率",
    "Numerical Analysis": "数值分析",
    "Computational Mechanics": "计算力学",
    "Energy": "能源",
    "Environmental Engineering": "环境工程",
    "Management Science and Operations Research": "管理科学与运筹学",
    "Marketing": "市场营销",
    "Strategy, Management and Accounting": "战略、管理与会计",
    "Business and International Management": "商业与国际管理",
    "Cognitive Science": "认知科学",
    "Sociology and Political Science": "社会与政治学",
    "Developmental and Educational Psychology": "发展与教育心理学",
    "Public Health, Environmental and Occupational Health":
        "公共卫生与环境职业健康",
    "Biochemistry": "生物化学",
    "Molecular Biology": "分子生物学",
    "Atomic and Molecular Physics, and Optics": "原子分子物理与光学",
    "General Medicine": "临床医学（综合）",
    "Organic Chemistry": "有机化学",
    "Mechanics of Materials": "材料力学",
    "Industrial and Manufacturing Engineering": "工业与制造工程",
    "Safety, Risk, Reliability and Quality": "安全、风险与质量",
    "Renewable Energy, Sustainability and the Environment":
        "可再生能源与可持续环境",
    "Building and Construction": "建筑与土木",
    "Transportation": "交通运输",
}


def topic_bilingual(tp: dict) -> dict:
    """给 OpenAlex 领域标注的 field / subfield 层附中文名。"""
    out = dict(tp)
    for layer in ("field", "subfield"):
        zh = _FIELD_ZH.get(out.get(layer) or "")             or _SUBFIELD_ZH.get(out.get(layer) or "")
        if zh:
            out[layer + "_zh"] = zh
    return out


_GENERIC = {
    "this", "that", "these", "those", "paper", "work", "study", "approach",
    "method", "methods", "model", "models", "propose", "proposed", "results",
    "result", "show", "shows", "shown", "experiment", "experiments",
    "experimental", "evaluation", "performance", "system", "systems",
    "problem", "which", "their", "when", "where", "while", "such", "also",
    "than", "then", "into", "over", "between", "each", "other", "more",
    "most", "well", "only", "our", "its", "have", "has", "are", "were",
    "been", "can", "may", "will", "one", "two", "three", "first", "second",
    "third", "however", "there", "them", "they", "what", "with", "from",
    "both", "some", "each", "same", "time", "case", "cases", "number",
    "large", "small", "high", "low", "new", "novel", "different", "given",
    "section", "figure", "table", "note", "terms", "term", "data",
    # 机构与出版信息常出现在标题附近，作检索词没有指向性
    "university", "institute", "institutes", "department", "college",
    "school", "lab", "laboratory", "china", "usa", "journal",
    "conference", "proceedings", "arxiv", "ieee", "acm", "press",
    "beijing", "shanghai", "posts", "telecom", "univ", "email",
    "corresponding", "author", "authors", "abstract",
}


def _local_keywords(paper, top: int = 8) -> list[str]:
    """从论文真实正文抽高频术语，作搜索关键词的兜底来源。

    纯 n-gram 频率统计：不经模型、不经词典推荐，每个词都出自 PDF。
    """
    from collections import Counter

    corpus = paper.title or ""
    body = " ".join(s.text for s in paper.sections if not s.is_reference)[:20000]
    corpus += " " + body
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", corpus)]
    words = [w for w in words if w not in _STOP and w not in _GENERIC]
    uni = Counter(words)
    bi = Counter(zip(words, words))
    cands: list[tuple[str, int]] = []
    for (a, b), c in bi.items():
        if c >= 2 and a != b:  # (w,w) 型是解析噪声，不是术语
            cands.append((a + " " + b, c * 2))  # 二元术语更具检索指向性
    for w, c in uni.items():
        cands.append((w, c))
    cands.sort(key=lambda x: -x[1])
    out: list[str] = []
    for term, _c in cands:
        if term not in out:
            out.append(term)
        if len(out) >= top:
            break
    return [t.title() for t in out]


def _keywords(text: str) -> set[str]:
    return {w for w in _norm(text).split() if len(w) >= 4 and w not in _STOP}


@dataclass
class RefNode:
    """网络图的一个节点。provenance 指明它的来源——这比任何
    「我们保证不编」的声明都可靠：来源就是代码路径。"""

    title: str
    kind: str = "cited"
    """``cited``（本文引用）/ ``citing``（引用本文）。"""

    origin: str = "pdf"
    """``pdf``（参考文献列表原文）/ ``openalex``（API 返回）。"""

    year: int = 0
    venue: str = ""
    doi: str = ""
    cited_by: int = 0
    openalex_id: str = ""
    relevance: float = 0.0
    """0–1，与本文的相关度。计算值，``reasons`` 给出依据。"""

    reasons: list[str] = field(default_factory=list)

    ccf: str = ""
    """CCF 推荐目录等级（A/B/C），来自内置目录数据；未列入为空。"""

    impact: str = ""
    """期刊影响因子（目录数据自带），会议为空。"""

    topics: list = field(default_factory=list)
    """OpenAlex 的领域标注（field / subfield / topic + 权重）。"""

    title_zh: str = ""
    """标题的机翻中文；仅展示用，翻译失败为空（节点保留英文）。"""

    def to_dict(self) -> dict:
        out = {"title": self.title, "kind": self.kind, "origin": self.origin,
               "title_zh": self.title_zh,
               "year": self.year, "cited_by": self.cited_by,
               "relevance": round(self.relevance, 2), "reasons": self.reasons}
        if self.doi:
            out["doi"] = self.doi
        if self.venue:
            out["venue"] = self.venue
        if self.openalex_id:
            out["openalex_id"] = self.openalex_id
        if self.topics:
            out["topics"] = self.topics
        return out


def translate_titles(titles: list[str], cfg, on_progress=None) -> dict:
    """批量机翻节点标题：一次模型调用，JSON 进出。

    只产出**显示用**的中文名；节点是谁、被引多少、相关度多少依旧
    全部来自数据库——机翻失败时节点照常展示英文标题。
    """
    from .llm import chat

    say = on_progress or (lambda _m: None)

    uniq = []
    seen = set()
    for s in titles:
        k = s.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            uniq.append(k)
    if not uniq:
        return {}
    listing = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(uniq))
    prompt = (
        "把下面这些学术论文标题翻译成中文（学术圈通用译法，没有通行译名就直译）。\n"
        "只输出 JSON：{\"translations\": [{\"i\": 序号, \"zh\": 中文标题}]}，"
        "不要输出其他内容。\n\n" + listing)
    say("节点标题机翻（一次调用）…")
    raw = chat([{"role": "user", "content": prompt}], cfg)
    m = None
    import re as _re
    mm = _re.search(r"\{.*\}", raw, _re.S)
    if mm:
        import json as _json
        try:
            data = _json.loads(mm.group())
            m = {uniq[int(x["i"]) - 1]: str(x.get("zh") or "").strip()
                 for x in (data.get("translations") or [])
                 if str(x.get("i", "")).strip().isdigit()}
        except Exception:  # noqa: BLE001
            m = None
    return m or {}


def _dedup_nodes(nodes: list[RefNode]) -> list[RefNode]:
    """同一篇文献在库里常有两条记录（arXiv 版 + 正式版），按标题归并，
    保留被引高、元数据全的那条——图上不该长出两个 BERT。"""
    seen: dict[str, RefNode] = {}
    for n in nodes:
        key = _norm(n.title)[:80]
        old_n = seen.get(key)
        if old_n is None or (n.cited_by, bool(n.doi)) > (old_n.cited_by, bool(old_n.doi)):
            seen[key] = n
    return list(seen.values())


def build_network(
    paper,
    *,
    max_nodes: int = 24,
    on_progress=None,
    cfg=None,
) -> dict:
    """构建一篇论文的引用网络。

    返回的 payload 直接嵌入报告：

    ``{"center": {...}, "nodes": [...RefNode.to_dict()],
       "stats": {...}, "unavailable": 空串或原因}``

    任何一步网络失败都不抛异常——降级成 ``unavailable`` 说明，
    报告的其余部分照常可用。
    """
    say = on_progress or (lambda _m: None)
    center_title = paper.title or ""

    api = OpenAlex()
    center = api.match_title(center_title) if center_title else None
    if center:
        say(f"中心论文已收录：{center['title'][:50]}")
    else:
        say("中心论文未收录（新论文常见），以 PDF 参考文献为骨架")

    # ---- cited：References 切条 → 标题匹配 ----
    say("解析参考文献列表 …")
    refs_text = "\n".join(s.text for s in paper.sections if s.is_reference)
    entries = split_references(refs_text)
    say(f"参考文献 {len(entries)} 条，逐条匹配 OpenAlex …")

    kw_center = _keywords(center_title)
    cited: list[RefNode] = []
    matched = 0
    for entry in entries:
        title = extract_title(entry)
        if len(title) < 12:
            continue
        node = RefNode(title=title, kind="cited", origin="pdf")
        m = api.match_title(title)
        if m:
            matched += 1
            node.openalex_id = m["openalex_id"]
            node.doi = m["doi"]
            node.title = m["title"] or node.title
            node.year = m["year"]
            node.cited_by = m["cited_by"]
            node.venue = m["venue"]
            node.topics = [topic_bilingual(tp)
                           for tp in (m.get("topics") or [])]
            # 相关度①：标题关键词与本文重合
            kw_node = _keywords(node.title)
            overlap = kw_center & kw_node
            kw_score = len(overlap) / max(len(kw_center), 1)
            node.relevance += 0.45 * min(kw_score * 2.5, 1.0)
            if overlap:
                node.reasons.append(f"与本文共享关键词：{('、'.join(sorted(overlap)[:5]))}")
            # 相关度②：与中心论文的共引（中心未收录时算不出，跳过）
            if center and center.get("refs"):
                shared = len({r.rsplit('/', 1)[-1] for r in m["refs"]}
                             & {r.rsplit('/', 1)[-1] for r in center["refs"]})
                if shared:
                    node.relevance += 0.35 * min(shared / 8, 1.0)
                    node.reasons.append(f"与本文共享 {shared} 篇共同引用")
            # 相关度③：影响力（log 缩放，1 万被引≈1）
            import math
            node.relevance += 0.20 * min(math.log10(node.cited_by + 1) / 4, 1.0)
            if node.cited_by:
                node.reasons.append(f"被引 {node.cited_by} 次")
        else:
            # 未命中：保留 PDF 原文条目，相关度给中性值
            node.relevance = 0.15
            node.reasons.append("仅见于本文参考文献列表（未在 OpenAlex 收录）")
            ym = re.search(r"\b(19|20)\d{2}\b", entry)
            if ym:
                node.year = int(ym.group())
        cited.append(node)

    # ---- citing：谁引用了本文（仅当中心被收录）----
    citing: list[RefNode] = []
    if center:
        say("查询引用本文的后续工作 …")
        for w in api.citing(center["openalex_id"]):
            node = RefNode(title=w["title"], kind="citing", origin="openalex",
                           year=w["year"], venue=w["venue"], doi=w["doi"],
                           cited_by=w["cited_by"], openalex_id=w["openalex_id"])
            kw_node = _keywords(node.title)
            overlap = kw_center & kw_node
            kw_score = len(overlap) / max(len(kw_center), 1)
            node.relevance = 0.55 * min(kw_score * 2.5, 1.0) + 0.25
            if overlap:
                node.reasons.append(f"与本文共享关键词：{('、'.join(sorted(overlap)[:5]))}")
            recency = max(0, 1 - max(0, node.year - 2020) / 10)
            node.relevance += 0.20 * (recency if node.year else 0.5)
            node.reasons.append(f"{node.year} 年发表，直接引用了本文")
            node.topics = [topic_bilingual(tp)
                           for tp in (w.get("topics") or [])]
            citing.append(node)
    else:
        say("中心未收录，跳过「引用本文」方向")

    # ---- 筛选：相关度×影响力综合排序，cited 与 citing 各取一半 ----
    def impact_key(n: RefNode):
        import math
        return (n.relevance, min(math.log10(n.cited_by + 1) / 4, 1.0))

    cited.sort(key=impact_key, reverse=True)
    citing.sort(key=impact_key, reverse=True)

    cited = _dedup_nodes(cited)
    citing = _dedup_nodes(citing)

    n_citing = min(len(citing), max_nodes // 2)
    n_cited = min(len(cited), max_nodes - n_citing)
    picked = sorted(cited[:n_cited] + citing[:n_citing],
                    key=lambda n: n.relevance, reverse=True)

    # 节点标题的中文是**机翻呈现层**：节点是谁、被引多少、相关度多少
    # 仍然全部来自数据库；翻译失败静默降级为英文。
    if cfg is not None:
        try:
            zh_map = translate_titles([n.title for n in picked], cfg,
                                      on_progress=say)
            for n in picked:
                n.title_zh = zh_map.get(n.title, "")
        except Exception:  # noqa: BLE001 —— 翻译是增强项
            say("  标题翻译失败，节点保留英文标题")

    stats = {
        "reference_entries": len(entries),
        "matched": matched,
        "citing_available": len(citing),
        "nodes": len(picked),
        "openalex_reachable": api.ok,
    }
    say(f"网络就绪：命中 {matched}/{len(entries)}，"
        f"后续工作 {len(citing)}，取 {len(picked)} 个节点")

    # 搜索关键词：不是推荐文章——给用户拿去自己搜的真实词条。
    # 优先 OpenAlex 对本文的官方 keywords 标注（收录后才有）；
    # 未收录或稀疏时，用本地 n-gram 高频术语兜底。都不经过模型。
    oa_kw = list((center or {}).get("keywords") or [])
    seen = {k.lower() for k in oa_kw}
    search_terms = (oa_kw + [x for x in _local_keywords(paper)
                             if x.lower() not in seen])[:8]

    return {
        "center": {
            "title": center_title,
            "year": (center or {}).get("year", 0) if center else 0,
            "doi": (center or {}).get("doi", "") if center else "",
            "matched": bool(center),
            "topics": [topic_bilingual(tp)
                       for tp in (center or {}).get("topics") or []],
            "venue": (center or {}).get("venue", "") if center else "",
            "venue_preprint": bool((center or {}).get("venue_is_preprint"))
                              if center else False,
            "ccf": ccf_lookup((center or {}).get("venue", "")).get("r", "")
                   if center and ccf_lookup((center or {}).get("venue", "")) else "",
            "search_terms": search_terms,
        },
        "nodes": [n.to_dict() for n in picked],
        "stats": stats,
        "unavailable": "" if api.ok else
                       "OpenAlex 无法访问（检查网络或代理），文献网络暂缺",
    }
