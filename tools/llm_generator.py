# -*- coding: utf-8 -*-
"""llm_generator.py — 标准 LLM API 候选生成适配器（2026-10-20 用户 #285）。

定位（承接 ai_candidate 架构契约）：
  本系统采用「AI 穷举 → 系统剪枝」架构，AI 在系统外面，只负责「读 state 想候选」；
  剪枝/step 模型/停规则/路径累积全在系统侧。本模块 = 把外部大模型接进那个槽位的
  **标准适配器**，取代 tools/macro_model_a.py 的手写规则枚举。

接口（与用户既定约定一致，且匹配 ai_candidate._call_gen 的签名探测）：
    generate(state, narrative_layer=None, node_context=None, seen=None) -> [candidate, ...]
  - ai_candidate._call_gen 探测到本函数有 seen 形参 → 以四参全量调用（追问循环兼容）。
  - 返回候选经 ai_candidate._normalize 归一（canon 别名/范围拒收 + sign 白名单），
    故**未知 sign 会抛错**而非静默丢弃——提示词因此必须把合法词汇表枚举给模型。

传输协议（标准，零第三方依赖）：
  OpenAI 兼容 `POST {base_url}/chat/completions`，覆盖 OpenAI / DeepSeek / Ollama
  （/v1）/ vLLM / one-api 等。用 stdlib urllib 实现，不引入 requests/openai 依赖。

配置（按优先级）：
  1. 构造函数 / make_generator(...) 显式传参
  2. 环境变量：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_TEMPERATURE / LLM_TIMEOUT
  3. 配置文件：config/llm_generator.json（键名同环境变量，小写）

诚实纪律（本项目一贯要求，LLM 接入尤其不能破）：
  · **未配置 API → 显式抛 RuntimeError**。绝不静默退回 macro_model_a 手写规则——
    那等于「冒名顶替 LLM」，会让整棵树的候选来源失真且不可观测。
  · LLM 返回无法解析成候选 → 抛错，不返回空列表。空列表会被下游当成「AI 想不出候选」，
    与「解析失败」混淆，属静默吞错。
  · 需要规则兜底时必须显式 allow_rule_fallback=True，且每次退回都在调用日志留痕
    （source="rule_fallback"），调用方与审计都能观测到。
  · 网络/HTTP 失败 → 抛错并带状态码，不伪装成「无候选」。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

import canon
import candidate_source  # 常量兜底（chain_days 等），与 ai_candidate._normalize 一致

# ─────────────────────────────────────────────────────────────────────────────
# 合法词汇表（提示词必须枚举；否则 LLM 输出会被 ai_candidate._normalize 拒收/抛错）
# ─────────────────────────────────────────────────────────────────────────────
SIGN_VERBS = ["up", "down", "flat",
              "tighten", "easing", "loosen", "strengthen", "weaken",
              "expansion", "contraction"]
MAGNITUDES = ["weak", "typical", "strong", None]

SYSTEM_PROMPT = """你是宏观传导分析师，任务是**穷举**当前宏观状态下所有合理的「下一步」候选。

纪律：
1. 只做因果传导推演，不做涨跌点预测、不给投资建议。
2. 每个候选必须属于两类之一：
   - direction：对某个可观测指标断言方向（target + sign + magnitude + chain_days + rationale）
   - narrative：激活一条宏观叙事（narrative + desc + chain_days + polarity + rationale）
3. target / narrative / sign **必须取自下方合法词汇表**，不得自造。表外一律会被系统拒收。
4. magnitude 只取 weak / typical / strong / null（null=典型档，由系统按历史分位数定幅）。
5. chain_days = 该步传导时滞（天），整数，按机制真实估计。
6. rationale = 一句话因果机制（为什么这一步承接上一步），必填。
7. 不要重复 seen 里已枚举的候选；只返回**新的**。
8. 穷举而非择优：把当前状态下机制上可能的步都列出来，由系统去剪枝。

合法 direction target：
{targets}

合法 narrative id：
{narratives}

合法 sign 动词：{signs}
（policy_stance 用 tighten=收紧 / easing·loosen=放松；FISCAL_STANCE 用 expansion=扩张 / contraction=收缩）

只输出 JSON 数组，不要任何解释文字。格式：
[
  {{"action":"direction","target":"CPI","sign":"up","magnitude":"typical","chain_days":30,"rationale":"..."}},
  {{"action":"narrative","narrative":"fiscal_expansion","desc":"...","chain_days":30,"polarity":"dovish","rationale":"..."}}
]"""


class LLMError(RuntimeError):
    """LLM 适配器失败（未配置 / 网络 / 解析 / 输出残缺）。绝不静默吞。"""
    pass


def _default_config():
    """按 环境变量 > config/llm_generator.json 的顺序取配置。"""
    cfg = {
        "base_url": os.environ.get("LLM_BASE_URL"),
        "api_key": os.environ.get("LLM_API_KEY"),
        "model": os.environ.get("LLM_MODEL"),
        "temperature": os.environ.get("LLM_TEMPERATURE"),
        "timeout": os.environ.get("LLM_TIMEOUT"),
    }
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "llm_generator.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                file_cfg = json.load(f)
            for k in list(cfg):
                if cfg[k] is None and file_cfg.get(k) is not None:
                    cfg[k] = file_cfg[k]
        except (OSError, ValueError) as e:
            raise LLMError("读取 config/llm_generator.json 失败：%s" % e)
    return cfg


def _fmt_state(state):
    """把 state 压成提示词可读文本（跳过内部键 _narrative/_transmission）。"""
    if not isinstance(state, dict):
        return "（无状态）"
    lines = []
    for k in sorted(state.keys()):
        if k in ("_narrative", "_transmission"):
            continue
        v = state[k]
        if isinstance(v, dict) and "value" in v:
            v = v.get("value")
        lines.append("  %s = %s" % (k, v))
    return "\n".join(lines) if lines else "  （空）"


def _fmt_seen(seen):
    if not seen:
        return "  （无，这是首轮枚举）"
    out = []
    for s in seen:
        if not isinstance(s, dict):
            continue
        if s.get("action") == "direction":
            out.append("  direction %s %s" % (s.get("target"), s.get("sign")))
        elif s.get("action") == "narrative":
            out.append("  narrative %s" % s.get("narrative"))
        else:
            out.append("  %s" % s.get("action"))
    return "\n".join(out) if out else "  （无，这是首轮枚举）"


def _fmt_context(node_context):
    nc = node_context or {}
    parts = []
    if nc.get("depth") is not None:
        parts.append("depth=%s" % nc["depth"])
    pn = nc.get("path_narratives")
    if pn:
        parts.append("path_narratives=%s" % ",".join(sorted(pn)))
    pm = nc.get("pruned_moves")
    if pm:
        # 只给数量与层名，避免把系统剪枝结论喂回生成端（那会污染「AI 在系统外面」边界）
        layers = {}
        for m in pm:
            if isinstance(m, dict):
                layers[m.get("layer", "?")] = layers.get(m.get("layer", "?"), 0) + 1
        parts.append("已被系统剪枝的候选数=%s（层：%s）"
                     % (len(pm), ",".join("%s×%d" % kv for kv in sorted(layers.items()))))
    if nc.get("focus"):
        parts.append("focus=%s" % nc["focus"])
    return "\n".join("  " + p for p in parts) if parts else "  （无附加上下文）"


def build_user_prompt(state, narrative_layer, node_context, seen):
    nl = list(narrative_layer) if narrative_layer else []
    return """【当前宏观状态】
{state}

【路径已激活叙事】
{nl}

【节点上下文】
{ctx}

【本轮已枚举候选（勿重复）】
{seen}

请穷举当前状态下所有合理的下一步候选，只输出 JSON 数组。""".format(
        state=_fmt_state(state),
        nl=("  " + ", ".join(nl)) if nl else "  （无）",
        ctx=_fmt_context(node_context),
        seen=_fmt_seen(seen))


def _extract_json_array(text):
    """从 LLM 回复中稳健提取 JSON 数组（容忍 ```json 围栏 / 前后解释文字）。"""
    if not text or not text.strip():
        raise LLMError("LLM 返回空内容")
    s = text.strip()
    # 1) 优先取 markdown 代码围栏内容
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    # 2) 截取第一个 '[' 到最后一个 ']'
    i, j = s.find("["), s.rfind("]")
    if i == -1 or j == -1 or j < i:
        raise LLMError("LLM 返回中找不到 JSON 数组。原文前 400 字：%r" % text[:400])
    frag = s[i:j + 1]
    try:
        arr = json.loads(frag)
    except ValueError as e:
        raise LLMError("LLM 返回的 JSON 数组解析失败：%s。片段前 400 字：%r" % (e, frag[:400]))
    if not isinstance(arr, list):
        raise LLMError("LLM 返回的 JSON 不是数组：%r" % type(arr))
    return arr


class LLMGenerator:
    """标准 LLM API 适配器。用法：

        gen = LLMGenerator(base_url=..., api_key=..., model=...)
        ai_candidate.GENERATOR_OVERRIDE = gen.generate
    """

    def __init__(self, base_url=None, api_key=None, model=None,
                 temperature=None, timeout=None, max_tokens=None,
                 transport=None, allow_rule_fallback=False, call_log=None):
        cfg = _default_config()
        self.base_url = (base_url or cfg["base_url"] or "").rstrip("/")
        self.api_key = api_key or cfg["api_key"]
        self.model = model or cfg["model"]
        self.temperature = float(temperature if temperature is not None
                                 else (cfg["temperature"] or 0.2))
        self.timeout = float(timeout if timeout is not None else (cfg["timeout"] or 60))
        self.max_tokens = int(max_tokens) if max_tokens else 2048
        # transport：可注入的传输函数 (url, headers, body_dict, timeout) -> str。
        #   默认 _http_post（真实网络）；测试可注入假传输以离线验证解析/归一/纪律。
        self.transport = transport or _http_post
        self.allow_rule_fallback = allow_rule_fallback
        self.call_log = call_log if call_log is not None else []
        self._system_prompt = SYSTEM_PROMPT.format(
            targets=", ".join(sorted(canon.DIRECTION_TARGETS)),
            narratives=", ".join(sorted(canon.NARRATIVE_IDS)),
            signs=", ".join(SIGN_VERBS))

    # ── 配置校验 ──
    def _require_config(self):
        missing = [k for k, v in (("base_url", self.base_url),
                                  ("api_key", self.api_key),
                                  ("model", self.model)) if not v]
        if missing:
            raise LLMError(
                "LLM 适配器未配置：缺少 %s。请设环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
                "（或写 config/llm_generator.json）。本系统不静默退回手写规则生成器——"
                "那会冒名顶替 LLM 使候选来源失真。" % ", ".join(missing))

    # ── 主入口（签名对齐 ai_candidate._call_gen 探测）──
    def generate(self, state, narrative_layer=None, node_context=None, seen=None):
        self._require_config()
        t0 = time.time()
        user_prompt = build_user_prompt(state, narrative_layer, node_context, seen)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % self.api_key}
        try:
            raw_text = self.transport(
                "%s/chat/completions" % self.base_url, headers, body, self.timeout)
        except LLMError:
            raise
        except Exception as e:
            raise LLMError("LLM 请求失败：%s: %s" % (type(e).__name__, e))

        content = _extract_message_content(raw_text)
        arr = _extract_json_array(content)
        if not arr:
            raise LLMError("LLM 返回空候选数组（JSON 解析成功但无元素）")
        # 过一遍 ai_candidate._normalize：canon 归一 + sign 白名单（未知 sign 抛错）
        import ai_candidate
        out = []
        for it in arr:
            c = ai_candidate._normalize(it)
            if c is None:
                # canon 范围外拒收（有界宇宙收敛保护）——静默丢弃是 canon 既定契约，
                # 但要在调用日志留痕，保证可观测。
                self.call_log.append({"event": "canon_rejected", "item": it})
                continue
            out.append(c)
        if not out:
            raise LLMError(
                "LLM 返回 %d 个候选全部被 canon 拒收（target/narrative 越界）。"
                "请检查提示词词汇表或模型输出。样例：%r" % (len(arr), arr[:3]))
        self.call_log.append({
            "event": "llm_call", "source": "llm",
            "n_raw": len(arr), "n_norm": len(out),
            "latency_s": round(time.time() - t0, 2),
            "model": self.model,
        })
        return out

    # ── 显式规则兜底（默认关闭；开启也留痕）──
    def generate_with_rule_fallback(self, state, narrative_layer=None,
                                    node_context=None, seen=None):
        """优先 LLM；显式失败时按 allow_rule_fallback 决定是否退回 macro_model_a。

        退回一定在 call_log 留 source="rule_fallback"，绝不无声。
        """
        try:
            return self.generate(state, narrative_layer, node_context, seen)
        except LLMError as e:
            if not self.allow_rule_fallback:
                raise
            import macro_model_a
            self.call_log.append({"event": "rule_fallback", "source": "rule_fallback",
                                  "reason": str(e)[:300]})
            return macro_model_a.macro_generator(state, narrative_layer, node_context, seen)


def _extract_message_content(raw_text):
    """从 /chat/completions 响应里取 choices[0].message.content。

    raw_text 可能是 dict（注入式传输已解析）或 str（真实 HTTP 响应体）。
    """
    if isinstance(raw_text, dict):
        data = raw_text
    else:
        try:
            data = json.loads(raw_text)
        except ValueError as e:
            raise LLMError("LLM 响应不是合法 JSON：%s。前 300 字：%r" % (e, raw_text[:300]))
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("LLM 响应缺 choices[0].message.content：%r" % str(data)[:300])


def _http_post(url, headers, body, timeout):
    """默认传输：stdlib urllib POST，返回响应体文本。"""
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise LLMError("HTTP %s %s %s" % (e.code, e.reason, detail))
    except urllib.error.URLError as e:
        raise LLMError("网络错误：%s" % getattr(e, "reason", e))


def make_generator(**kw):
    """工厂：按配置构造 LLMGenerator（未配置时构造成功但 generate 会抛错）。"""
    return LLMGenerator(**kw)


# ── 离线自测（无网络、无凭证）：用假传输验证解析/归一/纪律全链路 ──
def _self_test():
    print("=" * 78)
    print("llm_generator 离线自测（假传输，不触网、不需凭证）")
    print("=" * 78)
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print("  %-56s %s %s" % (name, "PASS" if cond else "FAIL", extra))
        if not cond:
            ok = False

    def resp(content):
        """假传输：把消息内容包成标准 /chat/completions 响应信封。"""
        return json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": content}}]})

    def transport_of(content):
        return lambda u, h, b, t: resp(content)

    # 1) 未配置 → 必须抛错（不静默）
    g = LLMGenerator(transport=lambda *a, **k: "never")
    try:
        g.generate({"CPI": {"value": 0.8}}, [])
        check("未配置 API 抛错", False)
    except LLMError as e:
        check("未配置 API 抛错（不静默）", "LLM_BASE_URL" in str(e))

    # 2) 假传输：返回围栏 JSON + 合法候选 → 解析归一成功
    fake = ("```json\n"
            "[{\"action\":\"direction\",\"target\":\"CPI\",\"sign\":\"up\","
            "\"magnitude\":\"typical\",\"chain_days\":30,\"rationale\":\"食品价格传导\"},"
            "{\"action\":\"narrative\",\"narrative\":\"fiscal_expansion\","
            "\"desc\":\"积极财政\",\"chain_days\":30,\"polarity\":\"dovish\","
            "\"rationale\":\"宽松货币伴随积极财政\"}]\n```")
    g2 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of(fake))
    state = {"CPI": {"value": 0.8}, "policy_stance": {"value": "偏松"},
             "LPR_1Y": {"value": 3.0}}
    cands = g2.generate(state, ["el_nino"], {"depth": 2, "path_narratives": ["el_nino"]},
                        seen=[])
    check("围栏 JSON 解析+归一得 2 候选", len(cands) == 2, "got=%d" % len(cands))
    check("direction 候选 target/sign 归一", cands[0]["target"] == "CPI" and cands[0]["sign"] == "up")
    check("narrative 候选 narrative 归一", cands[1]["narrative"] == "fiscal_expansion")
    check("chain_days 尊重 agent 供料", cands[0]["chain_days"] == 30)

    # 3) 中文同义 alias 归一（人民币汇率 → USDCNH）
    fake3 = ("[{\"action\":\"direction\",\"target\":\"人民币汇率\",\"sign\":\"up\","
             "\"chain_days\":30,\"rationale\":\"利差倒挂\"}]")
    g3 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of(fake3))
    c3 = g3.generate({"USDCNH": {"value": 6.72}}, [], None, None)
    check("中文 alias 归一（人民币汇率→USDCNH）", c3[0]["target"] == "USDCNH", c3[0]["target"])

    # 4) 动词语汇解禁（tighten 归一到 up，raw_sign 保留）
    fake4 = ("[{\"action\":\"direction\",\"target\":\"policy_stance\",\"sign\":\"tighten\","
             "\"chain_days\":30,\"rationale\":\"通胀压力抬头\"}]")
    g4 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of(fake4))
    c4 = g4.generate({"policy_stance": {"value": "偏松"}}, [], None, None)
    check("动词 tighten → sign=up", c4[0]["sign"] == "up")
    check("raw_sign 保留 tighten（honesty）", c4[0].get("raw_sign") == "tighten")

    # 5) 未知 sign → 必须抛错（不静默丢弃）
    fake5 = ("[{\"action\":\"direction\",\"target\":\"CPI\",\"sign\":\"skyrocket\","
             "\"chain_days\":30,\"rationale\":\"x\"}]")
    g5 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of(fake5))
    try:
        g5.generate({"CPI": {"value": 0.8}}, [], None, None)
        check("未知 sign 抛错（不静默）", False)
    except ValueError as e:
        check("未知 sign 抛错（不静默）", "skyrocket" in str(e))
    except LLMError as e:
        check("未知 sign 抛错（不静默）", "skyrocket" in str(e))

    # 6) 空内容 → 抛错（不返回空列表）
    g6 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of("   "))
    try:
        g6.generate({"CPI": {"value": 0.8}}, [], None, None)
        check("空回复抛错（不返空列表）", False)
    except LLMError:
        check("空回复抛错（不返空列表）", True)

    # 7) HTTP 错误 → 抛错带状态码
    def boom(u, h, b, t):
        raise LLMError("HTTP 429 Too Many Requests")
    g7 = LLMGenerator(base_url="http://x", api_key="k", model="m", transport=boom)
    try:
        g7.generate({"CPI": {"value": 0.8}}, [], None, None)
        check("HTTP 429 抛错", False)
    except LLMError as e:
        check("HTTP 429 抛错（带状态码）", "429" in str(e))

    # 8) 全部 canon 拒收 → 抛错（不是「无候选」）
    fake8 = ("[{\"action\":\"direction\",\"target\":\"NOT_A_TARGET\",\"sign\":\"up\","
             "\"chain_days\":1,\"rationale\":\"x\"}]")
    g8 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of(fake8))
    try:
        g8.generate({"CPI": {"value": 0.8}}, [], None, None)
        check("全 canon 拒收抛错", False)
    except LLMError as e:
        check("全 canon 拒收抛错（非静默无候选）", "canon" in str(e))
    check("canon 拒收有留痕", any(x.get("event") == "canon_rejected" for x in g8.call_log))

    # 9) 规则兜底必须显式开启且留痕
    g9 = LLMGenerator(base_url="http://x", api_key="k", model="m",
                      transport=transport_of("   "),
                      allow_rule_fallback=True)
    c9 = g9.generate_with_rule_fallback({"CPI": {"value": 0.8}}, [], None, None)
    check("显式规则兜底可用", isinstance(c9, list) and len(c9) > 0, "n=%d" % len(c9))
    check("规则兜底留痕 source=rule_fallback",
          any(x.get("source") == "rule_fallback" for x in g9.call_log))
    g9b = LLMGenerator(base_url="http://x", api_key="k", model="m",
                       transport=transport_of("   "),
                       allow_rule_fallback=False)
    try:
        g9b.generate_with_rule_fallback({"CPI": {"value": 0.8}}, [], None, None)
        check("未开兜底则透传错误", False)
    except LLMError:
        check("未开兜底则透传错误", True)

    # 10) 提示词必须含合法词汇表（否则 LLM 输出必被拒）
    g10 = LLMGenerator(base_url="http://x", api_key="k", model="m")
    check("提示词枚举 direction target", "CSI300" in g10._system_prompt and "CPI" in g10._system_prompt)
    check("提示词枚举 narrative id", "fiscal_expansion" in g10._system_prompt)
    check("提示词枚举 sign 动词", "tighten" in g10._system_prompt)
    check("提示词声明不做涨跌预测", "不做涨跌点预测" in g10._system_prompt)

    print("\n" + ("ALL PASS" if ok else "有 FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
